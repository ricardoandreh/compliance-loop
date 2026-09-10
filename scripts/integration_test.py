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


def wait_quiesce(timeout=60):
    """Wait until no executions are RUNNING.

    Teardown must not delete a bucket while re-evaluations (triggered by the
    remediation's own CloudTrail events) are in flight — they would hit
    NoSuchBucket, classify S_error and open spurious GitHub issues.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = sfn.list_executions(stateMachineArn=SFN_ARN, statusFilter="RUNNING", maxResults=5)
        if not resp["executions"]:
            return True
        time.sleep(3)
    log("wait_quiesce timed out; executions still RUNNING", "WARN")
    return False


def delete_bucket(name):
    wait_quiesce()
    # In-flight CloudTrail events may still spawn executions for a few
    # seconds after the last one finished; give them a chance to appear.
    time.sleep(8)
    wait_quiesce()
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
            "cloudtrail_events": {"PutBucketTagging"},
            "rego_policy_package": "s3_public_access",
            "resource_type": "aws_s3_bucket", "severity": "LOW", "policy_version": "v1.0",
        })
        log("Test control inserted, waiting for controls-sync...")
        time.sleep(15)

        pattern = json.loads(events_client.describe_rule(Name=rule_name)["EventPattern"])
        names = pattern["detail"]["eventName"]
        assert "PutBucketTagging" in names, f"Not in rule: {names}"
        metrics["t_sync_insert"] = round(time.time() - t_insert, 2)

        t_delete = time.time()
        table.delete_item(Key={"control_id": test_id})
        log("Test control deleted, waiting...")
        time.sleep(15)

        pattern = json.loads(events_client.describe_rule(Name=rule_name)["EventPattern"])
        names = pattern["detail"]["eventName"]
        assert "PutBucketTagging" not in names, f"Still in rule: {names}"
        metrics["t_sync_delete"] = round(time.time() - t_delete, 2)

        log(f"t_sync_insert={metrics['t_sync_insert']}s  t_sync_delete={metrics['t_sync_delete']}s", "TIME")
        log("C7 PASSED", "OK")
        return True, metrics
    finally:
        try:
            table.delete_item(Key={"control_id": test_id})
        except Exception:
            pass


# --- Per-Control Validation Tests ---

def test_ctrl_s3_public_block():
    """CTRL-01: ctrl-s3-public-block remediates to all true"""
    name = f"compliance-ctrl01-{int(time.time())}"
    metrics = {}
    create_bucket(name)
    try:
        t_event = time.time()
        set_public_access(name, block=False)
        log(f"Public access disabled on {name}")
        time.sleep(WAIT_CLOUDTRAIL)
        wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)

        cfg = get_public_access(name)
        assert all(cfg.values()), f"Not remediated: {cfg}"
        metrics["t_total"] = round(time.time() - t_event, 2)
        log(f"All 4 settings = true  t_total={metrics['t_total']}s", "TIME")
        log("CTRL-01 PASSED", "OK")
        return True, metrics
    finally:
        delete_bucket(name)


def test_ctrl_s3_ssl_only():
    """CTRL-02: ctrl-s3-ssl-only applies DenyNonSSLRequests policy"""
    name = f"compliance-ctrl02-{int(time.time())}"
    metrics = {}
    create_bucket(name)
    try:
        t_event = time.time()
        # DeleteBucketPolicy triggers ssl-only control
        try:
            s3.delete_bucket_policy(Bucket=name)
        except Exception:
            pass
        log(f"Policy deleted on {name}")
        time.sleep(WAIT_CLOUDTRAIL)
        wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)

        policy = json.loads(s3.get_bucket_policy(Bucket=name)["Policy"])
        sids = [st.get("Sid", "") for st in policy.get("Statement", [])]
        assert "DenyNonSSLRequests" in sids, f"SSL statement missing: {sids}"
        metrics["t_total"] = round(time.time() - t_event, 2)
        log(f"DenyNonSSLRequests present  t_total={metrics['t_total']}s", "TIME")
        log("CTRL-02 PASSED", "OK")
        return True, metrics
    finally:
        delete_bucket(name)


def test_ctrl_s3_versioning():
    """CTRL-03: ctrl-s3-versioning enables versioning"""
    name = f"compliance-ctrl03-{int(time.time())}"
    metrics = {}
    create_bucket(name)
    try:
        t_event = time.time()
        # PutBucketVersioning with Suspended triggers the control
        s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Suspended"})
        log(f"Versioning suspended on {name}")
        time.sleep(WAIT_CLOUDTRAIL)
        wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)

        resp = s3.get_bucket_versioning(Bucket=name)
        assert resp.get("Status") == "Enabled", f"Not enabled: {resp}"
        metrics["t_total"] = round(time.time() - t_event, 2)
        log(f"Versioning=Enabled  t_total={metrics['t_total']}s", "TIME")
        log("CTRL-03 PASSED", "OK")
        return True, metrics
    finally:
        delete_bucket(name)


def test_ctrl_s3_encryption():
    """CTRL-04: ctrl-s3-encryption classifies via the SYSTEM (not the runner)"""
    name = f"compliance-ctrl04-{int(time.time())}"
    arn = f"arn:aws:s3:::{name}"
    metrics = {}
    t_event = time.time()
    create_bucket(name)  # CreateBucket triggers ctrl-s3-encryption evaluation
    try:
        log(f"Checking encryption on {name}")
        time.sleep(WAIT_CLOUDTRAIL)
        wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)

        # Assert through the system's own classification, not a direct API
        # call from the runner's (admin) credentials — a direct call passes
        # even when the Analyze role cannot read the encryption config.
        violations = query_violations(arn)
        enc = [v for v in violations if v["control_id"] == "ctrl-s3-encryption"]
        assert enc, f"ctrl-s3-encryption never evaluated: {[v['control_id'] for v in violations]}"
        assert enc[-1]["state"] == "S_nominal", \
            f"Expected S_nominal (AES256 default), got {enc[-1]['state']}: {enc[-1].get('error', '')}"

        metrics["t_total"] = round(time.time() - t_event, 2)
        log(f"system classified S_nominal  t_total={metrics['t_total']}s", "TIME")
        log("CTRL-04 PASSED", "OK")
        return True, metrics
    finally:
        delete_bucket(name)


# --- Runner ---

TESTS = [
    ("C1", "Violação simples → remediação automática", test_c1),
    ("C2", "Exceção ativa → S_exception", test_c2),
    ("C3", "Exceção expira → remediação", test_c3),
    ("C4", "Bucket inexistente → escalação", test_c4),
    ("C5", "Violação por IaC → remediação", test_c5),
    ("C6", "Recurso já conforme → S_nominal", test_c6),
    ("C7", "Controls-sync → EventBridge dinâmico", test_c7),
    ("CTRL-01", "ctrl-s3-public-block", test_ctrl_s3_public_block),
    ("CTRL-02", "ctrl-s3-ssl-only", test_ctrl_s3_ssl_only),
    ("CTRL-03", "ctrl-s3-versioning", test_ctrl_s3_versioning),
    ("CTRL-04", "ctrl-s3-encryption", test_ctrl_s3_encryption),
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

    suite_start = time.time()
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

    # Audit: the suite must also assert the ABSENCE of unexpected failures.
    # A regression that only ADDS failed executions (e.g. teardown races,
    # broken side paths) would otherwise pass unnoticed — scenario asserts
    # only check for expected successes.
    wait_quiesce()
    failed_execs = []
    resp = sfn.list_executions(stateMachineArn=SFN_ARN, statusFilter="FAILED", maxResults=100)
    failed_execs = [e for e in resp["executions"] if e["startDate"].timestamp() > suite_start]
    expected_failed = 1 if "C4" in results else 0  # C4 ends FAILED by design
    unexpected = len(failed_execs) - expected_failed
    if unexpected > 0:
        log(f"AUDIT: {unexpected} unexpected FAILED execution(s) during the run "
            f"({len(failed_execs)} total, {expected_failed} expected)", "FAIL")
        for e in failed_execs[:10]:
            log(f"  {e['name']}", "FAIL")
        failed += 1
        results["AUDIT"] = "FAIL"
    else:
        log(f"AUDIT: no unexpected FAILED executions ({expected_failed} expected)", "OK")
        results["AUDIT"] = "PASS"

    passed_count = sum(1 for r in results.values() if r == "PASS")
    print(f"\n{passed_count}/{len(results)} passed")
    print(f"{'='*70}\n")

    if args.json:
        print(json.dumps({"results": results, "metrics": all_metrics}, indent=2))

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
