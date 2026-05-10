#!/usr/bin/env python3
"""
Integration test runner for MAPE-K Compliance Automation Loop.
Executes scenarios C1-C7 against the live AWS environment.
Collects timing metrics per hypothesis (H1: MTTR, H2: rastreabilidade, H3: tempo de exposição).

Usage:
  python3 scripts/integration_test.py                  # all tests
  python3 scripts/integration_test.py --test C1        # single test
  python3 scripts/integration_test.py --json           # JSON metrics output
"""

import argparse
import boto3
import json
import sys
import time
import uuid
from datetime import datetime, timezone

# --- Config ---

REGION = PREFIX = ENV = SFN_ARN = CONTROLS_TABLE = EXCEPTIONS_TABLE = VIOLATIONS_TABLE = None
s3 = sfn = dynamodb = None
WAIT_CLOUDTRAIL = 25


def init(region, prefix, env):
    global REGION, PREFIX, ENV, SFN_ARN, CONTROLS_TABLE, EXCEPTIONS_TABLE, VIOLATIONS_TABLE
    global s3, sfn, dynamodb
    REGION, PREFIX, ENV = region, prefix, env
    acct = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    SFN_ARN = f"arn:aws:states:{REGION}:{acct}:stateMachine:{PREFIX}-{PREFIX}-{ENV}"
    CONTROLS_TABLE = f"{PREFIX}-controls-{ENV}"
    EXCEPTIONS_TABLE = f"{PREFIX}-exceptions-{ENV}"
    VIOLATIONS_TABLE = f"{PREFIX}-violations-log-{ENV}"
    s3 = boto3.client("s3", region_name=REGION)
    sfn = boto3.client("stepfunctions", region_name=REGION)
    dynamodb = boto3.resource("dynamodb", region_name=REGION)


# --- Helpers ---

def log(msg, level="INFO"):
    colors = {"INFO": "\033[0m", "OK": "\033[32m", "FAIL": "\033[31m", "WARN": "\033[33m", "TIME": "\033[36m"}
    print(f"  {colors.get(level, '')}{msg}\033[0m")


def create_bucket(name):
    s3.create_bucket(Bucket=name)


def delete_bucket(name):
    try:
        s3.delete_bucket(Bucket=name)
    except Exception:
        pass


def set_public_access(name, block=False):
    s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
        "BlockPublicAcls": block, "IgnorePublicAcls": block,
        "BlockPublicPolicy": block, "RestrictPublicBuckets": block,
    })


def get_public_access(name):
    return s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]


def wait_for_executions(after_ts, expected_statuses, min_count=1, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = sfn.list_executions(stateMachineArn=SFN_ARN, maxResults=15)
        recent = [e for e in resp["executions"] if e["startDate"].timestamp() > after_ts]
        done = [e for e in recent if e["status"] in expected_statuses]
        running = [e for e in recent if e["status"] == "RUNNING"]
        if len(done) >= min_count and not running:
            return done
        time.sleep(3)
    return []


def get_execution_timing(exec_arn):
    """Extract start/stop timestamps and duration from execution history."""
    desc = sfn.describe_execution(executionArn=exec_arn)
    start = desc["startDate"].timestamp()
    stop = desc.get("stopDate", desc["startDate"]).timestamp()
    return {"start": start, "stop": stop, "duration_s": round(stop - start, 2), "status": desc["status"]}


def query_violations(resource_arn):
    table = dynamodb.Table(VIOLATIONS_TABLE)
    resp = table.query(
        IndexName="resource_arn-timestamp-index",
        KeyConditionExpression="resource_arn = :arn",
        ExpressionAttributeValues={":arn": resource_arn},
    )
    return sorted(resp["Items"], key=lambda x: x["timestamp"])


def put_exception(control_id, resource_arn, valid_until):
    eid = f"exc-test-{uuid.uuid4().hex[:8]}"
    dynamodb.Table(EXCEPTIONS_TABLE).put_item(Item={
        "exception_id": eid, "control_id": control_id,
        "resource_arn": resource_arn, "valid_until": valid_until,
        "justification": "Integration test", "approved_by": "test-approver",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    return eid


def delete_exception(eid):
    dynamodb.Table(EXCEPTIONS_TABLE).delete_item(Key={"exception_id": eid})


# --- Test Scenarios ---

def test_c1():
    """C1: Violação simples → remediação automática → convergência S_nominal"""
    name = f"compliance-inttest-c1-{int(time.time())}"
    arn = f"arn:aws:s3:::{name}"
    metrics = {}
    create_bucket(name)
    try:
        t_event = time.time()
        set_public_access(name, block=False)
        log(f"Violation triggered on {name}")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)
        assert execs, "No SUCCEEDED executions found"

        # Find remediation execution (the one that took longest = had Execute)
        timings = [(e, get_execution_timing(e["executionArn"])) for e in execs]
        remediation = max(timings, key=lambda x: x[1]["duration_s"])
        t_sfn_start = remediation[1]["start"]
        t_sfn_end = remediation[1]["stop"]

        metrics["t_detection"] = round(t_sfn_start - t_event, 2)
        metrics["t_remediation"] = remediation[1]["duration_s"]
        metrics["t_exposure"] = round(t_sfn_end - t_event, 2)

        cfg = get_public_access(name)
        assert all(cfg.values()), f"Bucket not remediated: {cfg}"

        violations = query_violations(arn)
        assert any(v["state"] == "S_violation" for v in violations)
        assert any(v.get("remediation_status") == "success" for v in violations)

        log(f"t_detection={metrics['t_detection']}s  t_remediation={metrics['t_remediation']}s  t_exposure={metrics['t_exposure']}s", "TIME")
        log("C1 PASSED", "OK")
        return True, metrics
    finally:
        delete_bucket(name)


def test_c2():
    """C2: Violação com exceção ativa → S_exception, sem remediação"""
    name = f"compliance-inttest-c2-{int(time.time())}"
    arn = f"arn:aws:s3:::{name}"
    metrics = {}
    create_bucket(name)
    eid = None
    try:
        eid = put_exception("ctrl-s3-public-block", arn, int(time.time()) + 86400)
        time.sleep(1)

        t_event = time.time()
        set_public_access(name, block=False)
        log(f"Violation with exception on {name}")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)
        assert execs, "No SUCCEEDED executions found"

        timing = get_execution_timing(execs[0]["executionArn"])
        metrics["t_classification"] = timing["duration_s"]

        cfg = get_public_access(name)
        assert not any(cfg.values()), f"Bucket remediated despite exception: {cfg}"

        violations = query_violations(arn)
        exc_entries = [v for v in violations if v["state"] == "S_exception" and v["control_id"] == "ctrl-s3-public-block"]
        assert exc_entries, "No S_exception in log"
        assert exc_entries[0].get("exception_id"), "Missing exception_id"
        assert exc_entries[0].get("approved_by"), "Missing approved_by"

        log(f"t_classification={metrics['t_classification']}s", "TIME")
        log("C2 PASSED", "OK")
        return True, metrics
    finally:
        if eid:
            delete_exception(eid)
        delete_bucket(name)


def test_c3():
    """C3: Exceção expira → Scheduler → re-avaliação → remediação"""
    name = f"compliance-inttest-c3-{int(time.time())}"
    arn = f"arn:aws:s3:::{name}"
    metrics = {}
    create_bucket(name)
    eid = None
    try:
        set_public_access(name, block=False)
        ttl = int(time.time()) + 60
        t_exception_created = time.time()
        eid = put_exception("ctrl-s3-public-block", arn, ttl)
        log(f"Exception expires at {ttl} (in 60s)")

        time.sleep(WAIT_CLOUDTRAIL)

        # Wait for expiry + scheduler buffer + execution
        remaining = max(0, ttl - time.time()) + 60
        log(f"Waiting {int(remaining)}s for expiry + scheduler + remediation...")
        time.sleep(remaining)

        execs = wait_for_executions(ttl - 5, ["SUCCEEDED"], min_count=1, timeout=60)

        cfg = get_public_access(name)
        assert all(cfg.values()), f"Bucket not remediated: {cfg}"

        violations = query_violations(arn)
        states = [v["state"] for v in violations]
        assert "S_exception" in states, f"No S_exception: {states}"
        assert "S_violation" in states, f"No S_violation: {states}"

        # Timing: scheduler precision
        if execs:
            sched_exec = get_execution_timing(execs[0]["executionArn"])
            metrics["t_scheduler_fire"] = round(sched_exec["start"] - ttl, 2)
            metrics["t_remediation_post_expiry"] = round(sched_exec["stop"] - ttl, 2)
            metrics["t_remediation"] = sched_exec["duration_s"]
            log(f"t_scheduler_fire={metrics['t_scheduler_fire']}s (after TTL)  t_remediation={metrics['t_remediation']}s  t_total_post_expiry={metrics['t_remediation_post_expiry']}s", "TIME")

        log("C3 PASSED", "OK")
        return True, metrics
    finally:
        if eid:
            delete_exception(eid)
        delete_bucket(name)


def test_c4():
    """C4: Bucket inexistente → escalação → FAILED"""
    bucket_name = f"bucket-inexistente-c4-{int(time.time())}"
    metrics = {}
    t_start = time.time()

    sfn.start_execution(
        stateMachineArn=SFN_ARN,
        input=json.dumps({
            "resource_id": bucket_name,
            "resource_arn": f"arn:aws:s3:::{bucket_name}",
            "account_id": boto3.client("sts", region_name=REGION).get_caller_identity()["Account"],
            "region": REGION,
            "event_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event_name": "PutBucketPublicAccessBlock",
        }),
    )
    log("Execution started with non-existent bucket")
    time.sleep(15)

    execs = wait_for_executions(t_start, ["FAILED"], min_count=1)
    assert execs, "No FAILED executions found"

    timing = get_execution_timing(execs[0]["executionArn"])
    metrics["t_escalation"] = timing["duration_s"]

    desc = sfn.describe_execution(executionArn=execs[0]["executionArn"])
    assert desc["error"] == "ComplianceEscalation"

    log(f"t_escalation={metrics['t_escalation']}s", "TIME")
    log("C4 PASSED", "OK")
    return True, metrics


def test_c5():
    """C5: Violação originada por IaC → remediação automática"""
    name = f"compliance-inttest-c5-{int(time.time())}"
    metrics = {}
    create_bucket(name)
    try:
        t_event = time.time()
        set_public_access(name, block=False)
        log(f"IaC-simulated violation on {name}")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)
        assert execs, "No SUCCEEDED executions found"

        timings = [(e, get_execution_timing(e["executionArn"])) for e in execs]
        remediation = max(timings, key=lambda x: x[1]["duration_s"])

        metrics["t_detection"] = round(remediation[1]["start"] - t_event, 2)
        metrics["t_remediation"] = remediation[1]["duration_s"]
        metrics["t_exposure"] = round(remediation[1]["stop"] - t_event, 2)

        cfg = get_public_access(name)
        assert all(cfg.values()), f"Bucket not remediated: {cfg}"

        log(f"t_detection={metrics['t_detection']}s  t_remediation={metrics['t_remediation']}s  t_exposure={metrics['t_exposure']}s", "TIME")
        log("C5 PASSED", "OK")
        return True, metrics
    finally:
        delete_bucket(name)


def test_c6():
    """C6: Recurso já conforme → S_nominal, sem Execute"""
    name = f"compliance-inttest-c6-{int(time.time())}"
    arn = f"arn:aws:s3:::{name}"
    metrics = {}
    create_bucket(name)
    try:
        t_event = time.time()
        set_public_access(name, block=True)
        log(f"Already compliant: {name}")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)
        assert execs, "No SUCCEEDED executions found"

        timing = get_execution_timing(execs[0]["executionArn"])
        metrics["t_classification"] = timing["duration_s"]

        violations = query_violations(arn)
        nominals = [v for v in violations if v["state"] == "S_nominal" and v["control_id"] == "ctrl-s3-public-block"]
        assert nominals, "No S_nominal in log"
        assert not any(v.get("remediation_status") for v in nominals), "Execute invoked on compliant resource"

        log(f"t_classification={metrics['t_classification']}s", "TIME")
        log("C6 PASSED", "OK")
        return True, metrics
    finally:
        delete_bucket(name)


def test_c7():
    """C7: Atualização dinâmica do EventBridge via controls-sync"""
    table = dynamodb.Table(CONTROLS_TABLE)
    test_id = f"ctrl-test-c7-{int(time.time())}"
    metrics = {}
    events_client = boto3.client("events", region_name=REGION)
    rule_name = f"{PREFIX}-s3-public-access-block-{ENV}"

    try:
        t_insert = time.time()
        table.put_item(Item={
            "control_id": test_id, "description": "Integration test",
            "cloudtrail_events": {"PutBucketEncryption"},
            "rego_policy_package": "s3_public_access",
            "resource_type": "aws_s3_bucket", "severity": "LOW", "policy_version": "v1.0",
        })
        log("Test control inserted, waiting for controls-sync...")
        time.sleep(15)

        pattern = json.loads(events_client.describe_rule(Name=rule_name)["EventPattern"])
        names = pattern["detail"]["eventName"]
        assert "PutBucketEncryption" in names, f"Not in rule: {names}"
        metrics["t_sync_insert"] = round(time.time() - t_insert, 2)

        t_delete = time.time()
        table.delete_item(Key={"control_id": test_id})
        log("Test control deleted, waiting...")
        time.sleep(15)

        pattern = json.loads(events_client.describe_rule(Name=rule_name)["EventPattern"])
        names = pattern["detail"]["eventName"]
        assert "PutBucketEncryption" not in names, f"Still in rule: {names}"
        metrics["t_sync_delete"] = round(time.time() - t_delete, 2)

        log(f"t_sync_insert={metrics['t_sync_insert']}s  t_sync_delete={metrics['t_sync_delete']}s", "TIME")
        log("C7 PASSED", "OK")
        return True, metrics
    finally:
        try:
            table.delete_item(Key={"control_id": test_id})
        except Exception:
            pass


# --- Runner ---

TESTS = [
    ("C1", "Violação simples → remediação automática", test_c1),
    ("C2", "Exceção ativa → S_exception", test_c2),
    ("C3", "Exceção expira → remediação", test_c3),
    ("C4", "Bucket inexistente → escalação", test_c4),
    ("C5", "Violação por IaC → remediação", test_c5),
    ("C6", "Recurso já conforme → S_nominal", test_c6),
    ("C7", "Controls-sync → EventBridge dinâmico", test_c7),
]


def main():
    parser = argparse.ArgumentParser(description="MAPE-K Integration Tests")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--prefix", default="compliance-loop")
    parser.add_argument("--env", default="dev")
    parser.add_argument("--test", help="Run specific test (e.g., C1)")
    parser.add_argument("--json", action="store_true", help="Output metrics as JSON")
    args = parser.parse_args()

    init(args.region, args.prefix, args.env)

    tests = TESTS
    if args.test:
        tests = [(n, d, f) for n, d, f in TESTS if n.upper() == args.test.upper()]
        if not tests:
            print(f"Unknown test: {args.test}")
            sys.exit(1)

    print(f"\n{'='*70}")
    print(f"MAPE-K Integration Tests — {args.prefix}-{args.env}")
    print(f"{'='*70}\n")

    results = {}
    all_metrics = {}

    for name, desc, fn in tests:
        print(f"[{name}] {desc}")
        try:
            passed, metrics = fn()
            results[name] = "PASS" if passed else "FAIL"
            all_metrics[name] = metrics
        except Exception as e:
            log(f"FAILED: {e}", "FAIL")
            results[name] = "FAIL"
            all_metrics[name] = {"error": str(e)}
        print()

    # Summary
    print(f"{'='*70}")
    print("Results:")
    for name, result in results.items():
        icon = "✅" if result == "PASS" else "❌"
        m = all_metrics.get(name, {})
        timing_str = "  ".join(f"{k}={v}s" for k, v in m.items() if isinstance(v, (int, float)))
        print(f"  {icon} {name}: {result}  {timing_str}")

    # Hypothesis summary
    h1_tests = {k: v for k, v in all_metrics.items() if "t_remediation" in v}
    h3_tests = {k: v for k, v in all_metrics.items() if "t_exposure" in v}

    if h1_tests:
        print(f"\n  H1 (MTTR):")
        for k, v in h1_tests.items():
            print(f"    {k}: t_remediation = {v['t_remediation']}s")
        avg = round(sum(v["t_remediation"] for v in h1_tests.values()) / len(h1_tests), 2)
        print(f"    avg = {avg}s")

    if h3_tests:
        print(f"\n  H3 (Tempo de exposição):")
        for k, v in h3_tests.items():
            print(f"    {k}: t_exposure = {v['t_exposure']}s")
        avg = round(sum(v["t_exposure"] for v in h3_tests.values()) / len(h3_tests), 2)
        print(f"    avg = {avg}s")

    failed = sum(1 for r in results.values() if r == "FAIL")
    print(f"\n{len(results) - failed}/{len(results)} passed")
    print(f"{'='*70}\n")

    if args.json:
        print(json.dumps({"results": results, "metrics": all_metrics}, indent=2))

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
