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
import os
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

    global CLOCK_OFFSET
    CLOCK_OFFSET = measure_clock_offset()
    if abs(CLOCK_OFFSET) >= 1.0:
        log(f"local clock is {CLOCK_OFFSET:+.2f}s off AWS; metrics compensated", "WARN")


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


def wait_for_revocation_cascade(grace=30):
    """Wait out the chain that deleting a still-valid exception sets off.

    Removing a live grant is a revoke, not housekeeping: scheduler-sync
    re-evaluates the resource, which remediates it, and that remediation emits
    its own CloudTrail event and one more evaluation. The chain starts on a
    DynamoDB Stream, so wait_quiesce() on its own can return before the first
    execution even exists — and tearing the bucket down in that gap makes the
    late evaluation hit NoSuchBucket, classify S_error and open a GitHub issue
    for a bucket the test deleted on purpose.
    """
    deadline = time.time() + grace
    while time.time() < deadline:
        resp = sfn.list_executions(stateMachineArn=SFN_ARN, statusFilter="RUNNING", maxResults=5)
        if resp["executions"]:
            break
        time.sleep(2)
    wait_quiesce(timeout=120)
    time.sleep(8)
    wait_quiesce(timeout=120)


def close_escalation_issue(resource_id, control_id, timeout=75):
    """Close the GitHub issue this scenario opened on purpose.

    C4 escalates by design — a bucket that does not exist, so remediation fails
    and the loop raises an issue. That is the behaviour under test, but without
    cleanup every benchmark run leaves one more issue behind for a bucket that
    never existed; 71 had accumulated before this was added.

    The issue arrives asynchronously (SQS → Lambda → GitHub API), so poll for it
    rather than assume it is already there. Nothing here is fatal: the scenario's
    assertions have already passed by this point, and scripts/close_test_issues.py
    can always sweep whatever was missed.
    """
    try:
        import close_test_issues as gh
    except ImportError:
        log("close_test_issues not importable; issue left open", "WARN")
        return False

    title = f"[VIOLATION] {control_id} \u2014 {resource_id}"
    try:
        params = gh.github_params(REGION)
    except Exception as e:
        log(f"could not read GitHub params; issue left open ({e})", "WARN")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            match = [i for i in gh.list_open_issues(params) if i["title"] == title]
        except Exception as e:
            log(f"issue lookup failed; leaving it open ({e})", "WARN")
            return False
        if match:
            try:
                gh.close_issue(params, match[0]["number"])
                log(f"closed escalation issue #{match[0]['number']}", "OK")
                return True
            except Exception as e:
                log(f"could not close #{match[0]['number']} ({e})", "WARN")
                return False
        time.sleep(5)

    log(f"escalation issue for {resource_id} did not appear in {timeout}s", "WARN")
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


# --- Clock alignment ---
#
# The runner's clock and AWS's are independent, and this matters twice:
#
# 1. Metrics. t_detection and t_exposure subtract a LOCAL timestamp from an AWS
#    one (Step Functions startDate/stopDate). Any offset lands directly in the
#    result — a local clock running ahead makes the system look faster than it
#    is, which would bias H1/H3 in our own favour.
# 2. Correctness of the search. wait_for_executions keeps executions whose
#    startDate is later than a locally-taken reference. If the local clock runs
#    ahead, an execution AWS stamped moments after the reference is silently
#    dropped. Tests that trigger via CloudTrail absorb this in their ~8s
#    detection delay; C4, which calls StartExecution directly, does not — it
#    reported "No FAILED executions found" for an execution that had in fact
#    failed correctly 0.6s in.
#
# The Date response header has 1-second resolution and is truncated, so
# (local - header) always lands in [offset, offset + 1) and the minimum across
# samples is the best estimator, approaching the true offset from above. The
# residual is therefore bounded by roughly one second divided by the number of
# samples that happen to straddle a tick — good enough for metrics reported in
# seconds, but it does mean t_detection carries sub-second systematic
# uncertainty that no amount of repetition removes. Reported as a limitation
# rather than hidden.
CLOCK_OFFSET = 0.0


def measure_clock_offset(samples=10):
    """Seconds the local clock runs ahead of AWS (negative if behind)."""
    import email.utils
    sts = boto3.client("sts", region_name=REGION)
    deltas = []
    for _ in range(samples):
        t0 = time.time()
        resp = sts.get_caller_identity()
        t1 = time.time()
        header = resp["ResponseMetadata"]["HTTPHeaders"].get("date")
        if not header:
            continue
        aws = email.utils.parsedate_to_datetime(header).timestamp()
        deltas.append((t0 + t1) / 2 - aws)
    return min(deltas) if deltas else 0.0


def aws_now():
    """Local clock expressed on AWS's timeline; use for anything compared
    against a Step Functions startDate/stopDate."""
    return time.time() - CLOCK_OFFSET


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


def pick_execution(execs, event_name=None, trigger=None, resource_id=None,
                   longest=False):
    """Pick the execution that corresponds to the step under test.

    Unrelated executions overlap in time, and taking execs[0] measured whichever
    the listing happened to return first. Two cases bit us:

    - C2/C6: creating the bucket emits CreateBucket, which fans out to every S3
      control and remediates the ones a fresh bucket fails (~13s). The violation
      the scenario actually triggers classifies in under a second. Both start
      within the same second, so t_classification came out as 0.4s on some runs
      and 13.2s on others for the same scenario, with nothing wrong.
    - C3: after the scheduler-triggered remediation (~5.9s), that remediation's
      own CloudTrail event used to cause one more evaluation (~0.5s). Picking
      the latter reported t_remediation as 0.47s and dragged H1's interval
      negative. The EventBridge rule now filters the Execute role, so that echo
      no longer exists; the selection stays as a guard in case the filter ever
      regresses, and assert_no_remediation_echo() is what detects that.

    Matching on fields of the execution input ties each metric to the event the
    scenario caused. Falls back to the most recent execution if nothing matches,
    so a scenario never silently loses its metric — but says so.
    """
    matches = []
    for e in execs:
        try:
            raw = sfn.describe_execution(executionArn=e["executionArn"]).get("input") or "{}"
            inp = json.loads(raw)
        except Exception:
            continue
        if event_name is not None and inp.get("event_name") != event_name:
            continue
        if trigger is not None and inp.get("trigger") != trigger:
            continue
        if resource_id is not None and resource_id not in raw:
            continue
        matches.append(e)

    if matches:
        if not longest:
            return matches[0]
        # A single event name can cover several executions: the violation that
        # remediates (~5s) and the re-evaluations the remediation itself
        # triggers (~0.4s each). Filtering by event alone would pick whichever
        # the listing returned first — measuring a re-evaluation as if it were
        # the remediation. Taking the longest WITHIN the matching event is what
        # separates them; taking the longest across ALL events was the original
        # bug, since it picked the CreateBucket fan-out instead.
        return max(matches, key=lambda e: get_execution_timing(
            e["executionArn"])["duration_s"])

    wanted = {k: v for k, v in
              (("event_name", event_name), ("trigger", trigger)) if v is not None}
    log(f"no execution matched {wanted}; falling back to most recent", "WARN")
    return execs[0]


def executions_for(resource_id, since, max_items=120):
    """Every execution about this resource that started after `since`, with its
    parsed input. Unlike wait_for_executions, not limited to the newest 15."""
    found = []
    pages = sfn.get_paginator("list_executions").paginate(
        stateMachineArn=SFN_ARN, PaginationConfig={"MaxItems": max_items})
    for page in pages:
        for e in page["executions"]:
            if e["startDate"].timestamp() < since:
                return found
            raw = sfn.describe_execution(executionArn=e["executionArn"]).get("input") or "{}"
            if resource_id in raw:
                found.append({"arn": e["executionArn"], "status": e["status"],
                              "input": json.loads(raw)})
    return found


def verification_round(resource_id, origin_arn, since, timeout=120):
    """The single verification round a remediating detection round schedules.

    Waits for it to finish. Its status is the loop's own verdict: SUCCEEDED means
    every remediated control re-evaluated S_nominal; FAILED means at least one
    did not converge and was escalated instead of remediated again.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        rounds = [x for x in executions_for(resource_id, since)
                  if x["input"].get("mode") == "verify"
                  and x["input"].get("origin_execution") == origin_arn]
        if len(rounds) > 1:
            raise AssertionError(f"{len(rounds)} verification rounds for one detection round")
        if rounds and rounds[0]["status"] != "RUNNING":
            return rounds[0], get_execution_timing(rounds[0]["arn"])
        time.sleep(3)
    raise AssertionError(f"no finished verification round for {origin_arn} within {timeout}s")


def assert_no_remediation_echo(resource_id, since, remediation_only_events):
    """Remediation writes must not start executions any more.

    `remediation_only_events` are API calls the scenario itself never makes, so
    any execution they triggered came from the Execute role's own writes — the
    EventBridge identity filter has regressed (or the live rule was never
    regenerated by controls-sync after a deploy).
    """
    echoes = [x for x in executions_for(resource_id, since)
              if x["input"].get("event_name") in remediation_only_events]
    assert not echoes, (
        f"{len(echoes)} execution(s) started by the remediation's own events "
        f"({sorted({x['input']['event_name'] for x in echoes})}): the EventBridge "
        f"rule is not filtering the Execute role")


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
        t_event = aws_now()
        set_public_access(name, block=False)
        log(f"Violation triggered on {name}")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)
        assert execs, "No SUCCEEDED executions found"

        # Select by the event this scenario triggered, NOT by max(duration).
        # max() reliably picked the wrong execution: create_bucket emits
        # CreateBucket, which fans out to all four S3 controls and remediates
        # three of them serially in ~18.5s: versioning and ssl-only, which a
        # fresh bucket fails, plus public-block, which this test has already
        # switched off by the time the event is evaluated (both events arrive
        # together). The violation under test remediates one control in ~6s, so
        # max() reported the setup's fan-out as this scenario's MTTR — inflating
        # H1 by roughly 3x and attributing the fan-out's spread to it.
        chosen = pick_execution(execs, event_name="PutBucketPublicAccessBlock",
                                resource_id=name, longest=True)
        remediation = (chosen, get_execution_timing(chosen["executionArn"]))
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

        # The loop closes itself: one verification round, linked to this
        # detection round, re-evaluates what it remediated.
        verify, vt = verification_round(name, chosen["executionArn"], t_event - 60)
        assert verify["status"] == "SUCCEEDED", f"remediation did not converge: {verify['arn']}"
        metrics["t_verified"] = round(vt["stop"] - t_event, 2)
        confirmed = [v for v in query_violations(arn)
                     if v.get("round") == "verification" and v["state"] == "S_nominal"
                     and v.get("origin_execution") == chosen["executionArn"]]
        assert confirmed, "verification round left no S_nominal linked to the detection round"
        assert_no_remediation_echo(name, t_event - 60, {"PutBucketVersioning", "PutBucketPolicy"})

        log(f"t_detection={metrics['t_detection']}s  t_remediation={metrics['t_remediation']}s  t_exposure={metrics['t_exposure']}s  t_verified={metrics['t_verified']}s", "TIME")
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

        t_event = aws_now()
        set_public_access(name, block=False)
        log(f"Violation with exception on {name}")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)
        assert execs, "No SUCCEEDED executions found"

        chosen = pick_execution(execs, event_name="PutBucketPublicAccessBlock",
                                resource_id=name)
        metrics["t_classification"] = get_execution_timing(chosen["executionArn"])["duration_s"]

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
            # The grant is still valid here (24h), so removing it is a revoke
            # and triggers a full re-evaluation; let it converge before the
            # bucket disappears.
            delete_exception(eid)
            wait_for_revocation_cascade()
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
            chosen = pick_execution(execs, trigger="exception_ttl_expiry",
                                    resource_id=name)
            sched_exec = get_execution_timing(chosen["executionArn"])
            metrics["t_scheduler_fire"] = round(sched_exec["start"] - ttl, 2)
            metrics["t_remediation_post_expiry"] = round(sched_exec["stop"] - ttl, 2)
            metrics["t_remediation"] = sched_exec["duration_s"]
            verify, vt = verification_round(name, chosen["executionArn"], ttl - 5)
            assert verify["status"] == "SUCCEEDED", f"remediation did not converge: {verify['arn']}"
            metrics["t_verified_post_expiry"] = round(vt["stop"] - ttl, 2)
            log(f"t_scheduler_fire={metrics['t_scheduler_fire']}s (after TTL)  t_remediation={metrics['t_remediation']}s  t_total_post_expiry={metrics['t_remediation_post_expiry']}s  t_verified_post_expiry={metrics['t_verified_post_expiry']}s", "TIME")

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
    # StartExecution returns the ARN, so this scenario can poll the exact
    # execution instead of searching recent ones by start time. The search is
    # only needed when the trigger is indirect (CloudTrail), and here it was
    # actively harmful: the execution finishes in under a second, and any
    # clock skew pushed it outside the "startDate > t_start" filter.
    exec_arn = sfn.start_execution(
        stateMachineArn=SFN_ARN,
        input=json.dumps({
            "resource_id": bucket_name,
            "resource_arn": f"arn:aws:s3:::{bucket_name}",
            "account_id": boto3.client("sts", region_name=REGION).get_caller_identity()["Account"],
            "region": REGION,
            "event_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event_name": "PutBucketPublicAccessBlock",
        }),
    )["executionArn"]
    log("Execution started with non-existent bucket")

    deadline = time.time() + 90
    desc = sfn.describe_execution(executionArn=exec_arn)
    while desc["status"] == "RUNNING" and time.time() < deadline:
        time.sleep(2)
        desc = sfn.describe_execution(executionArn=exec_arn)
    assert desc["status"] == "FAILED", f"Expected FAILED, got {desc['status']}"

    assert desc["error"] == "ComplianceEscalation", \
        f"Expected ComplianceEscalation, got {desc.get('error')}"

    metrics["t_escalation"] = get_execution_timing(exec_arn)["duration_s"]

    log(f"t_escalation={metrics['t_escalation']}s", "TIME")
    log("C4 PASSED", "OK")

    # The escalation is the point of this scenario, so the issue it raises is a
    # real result — but it describes a bucket the test invented, so it should not
    # outlive the run.
    close_escalation_issue(bucket_name, "ctrl-s3-public-block")

    return True, metrics


def test_c5():
    """C5: Violação originada por IaC → remediação automática"""
    name = f"compliance-inttest-c5-{int(time.time())}"
    metrics = {}
    create_bucket(name)
    try:
        t_event = aws_now()
        set_public_access(name, block=False)
        log(f"IaC-simulated violation on {name}")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)
        assert execs, "No SUCCEEDED executions found"

        # Select by the event this scenario triggered, NOT by max(duration).
        # max() reliably picked the wrong execution: create_bucket emits
        # CreateBucket, which fans out to all four S3 controls and remediates
        # three of them serially in ~18.5s: versioning and ssl-only, which a
        # fresh bucket fails, plus public-block, which this test has already
        # switched off by the time the event is evaluated (both events arrive
        # together). The violation under test remediates one control in ~6s, so
        # max() reported the setup's fan-out as this scenario's MTTR — inflating
        # H1 by roughly 3x and attributing the fan-out's spread to it.
        chosen = pick_execution(execs, event_name="PutBucketPublicAccessBlock",
                                resource_id=name, longest=True)
        remediation = (chosen, get_execution_timing(chosen["executionArn"]))

        metrics["t_detection"] = round(remediation[1]["start"] - t_event, 2)
        metrics["t_remediation"] = remediation[1]["duration_s"]
        metrics["t_exposure"] = round(remediation[1]["stop"] - t_event, 2)

        cfg = get_public_access(name)
        assert all(cfg.values()), f"Bucket not remediated: {cfg}"

        verify, vt = verification_round(name, chosen["executionArn"], t_event - 60)
        assert verify["status"] == "SUCCEEDED", f"remediation did not converge: {verify['arn']}"
        metrics["t_verified"] = round(vt["stop"] - t_event, 2)
        assert_no_remediation_echo(name, t_event - 60, {"PutBucketVersioning", "PutBucketPolicy"})

        log(f"t_detection={metrics['t_detection']}s  t_remediation={metrics['t_remediation']}s  t_exposure={metrics['t_exposure']}s  t_verified={metrics['t_verified']}s", "TIME")
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
        t_event = aws_now()
        set_public_access(name, block=True)
        log(f"Already compliant: {name}")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)
        assert execs, "No SUCCEEDED executions found"

        chosen = pick_execution(execs, event_name="PutBucketPublicAccessBlock",
                                resource_id=name)
        metrics["t_classification"] = get_execution_timing(chosen["executionArn"])["duration_s"]

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
        t_event = aws_now()
        set_public_access(name, block=False)
        log(f"Public access disabled on {name}")
        time.sleep(WAIT_CLOUDTRAIL)
        wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)

        cfg = get_public_access(name)
        assert all(cfg.values()), f"Not remediated: {cfg}"
        metrics["t_total"] = round(aws_now() - t_event, 2)
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
        t_event = aws_now()
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
        metrics["t_total"] = round(aws_now() - t_event, 2)
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
        t_event = aws_now()
        # PutBucketVersioning with Suspended triggers the control
        s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Suspended"})
        log(f"Versioning suspended on {name}")
        time.sleep(WAIT_CLOUDTRAIL)
        wait_for_executions(t_event, ["SUCCEEDED"], min_count=1)

        resp = s3.get_bucket_versioning(Bucket=name)
        assert resp.get("Status") == "Enabled", f"Not enabled: {resp}"
        metrics["t_total"] = round(aws_now() - t_event, 2)
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
    t_event = aws_now()
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

        metrics["t_total"] = round(aws_now() - t_event, 2)
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
    parser.add_argument("--json-out", metavar="PATH",
                        help="Write the metrics JSON to PATH (consumed by benchmark.py)")
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

    suite_start = aws_now()
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

    payload = {"results": results, "metrics": all_metrics}
    if args.json:
        print(json.dumps(payload, indent=2))
    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w") as f:
            json.dump(payload, f, indent=2)
        log(f"metrics written to {args.json_out}", "OK")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
