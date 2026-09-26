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
    NoSuchBucket, fail to evaluate and open spurious GitHub issues.
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
    late evaluation hit NoSuchBucket, fail to evaluate and open a GitHub issue
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

    C4 and C8 escalate by design — a bucket that does not exist (C4, a system
    failure) and a remediation the Execute role is denied (C8). That is the
    behaviour under test, but without cleanup every benchmark run leaves more
    issues behind for fixtures the test invented; 71 had accumulated before this
    was added.

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


def wait_for_executions(needle, since, expected_statuses, min_count=1, timeout=90):
    """Executions about this scenario that started after `since`, once they finish.

    `needle` is matched against the execution input: the bucket name, or the
    event name for account-level resources that have no name of their own.
    Waits until at least `min_count` finished with one of `expected_statuses`
    and none of them is still RUNNING — which includes the verification rounds,
    since they carry the resource too.

    Replaces a listing of the 15 most recent executions across the whole state
    machine (design.md, L9): with remediations and verification rounds of other
    resources overlapping, the scenario's own execution could fall out of that
    window and fail the test without any fault in the system.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = executions_for(needle, since)
        done = [x for x in found if x["status"] in expected_statuses]
        if len(done) >= min_count and not any(x["status"] == "RUNNING" for x in found):
            return done
        time.sleep(3)
    return []


def pick_execution(execs, event_name=None, trigger=None, longest=False):
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
    scenario caused. Nothing matching is a test failure: the fallback this
    replaced took the most recent execution with only a warning, so a scenario
    could time an unrelated execution and still pass.
    """
    matches = [e for e in execs
               if (event_name is None or e["input"].get("event_name") == event_name)
               and (trigger is None or e["input"].get("trigger") == trigger)]
    wanted = {k: v for k, v in (("event_name", event_name), ("trigger", trigger)) if v is not None}
    assert matches, f"no execution matched {wanted} among {len(execs)} for this scenario"
    if not longest:
        return min(matches, key=lambda e: e["start"])
    # A single event name can cover several executions: the violation that
    # remediates (~5s) and the re-evaluations the remediation itself used to
    # trigger (~0.4s each). Taking the longest WITHIN the matching event is what
    # separates them; taking the longest across ALL events was the original bug,
    # since it picked the CreateBucket fan-out instead.
    return max(matches, key=lambda e: get_execution_timing(e["arn"])["duration_s"])


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
                              "start": e["startDate"].timestamp(),
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


def escalation_reasons(exec_arn):
    """The `reason` of every escalation an execution enqueued, in order."""
    reasons = []
    for ev in sfn.get_execution_history(executionArn=exec_arn, maxResults=1000)["events"]:
        sched = ev.get("taskScheduledEventDetails")
        if sched and sched.get("resourceType") == "sqs":
            body = json.loads(json.loads(sched["parameters"])["MessageBody"])
            reasons.append(body.get("reason"))
    return reasons


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

        execs = wait_for_executions(name, t_event, ["SUCCEEDED"])
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
        chosen = pick_execution(execs, event_name="PutBucketPublicAccessBlock", longest=True)
        remediation = (chosen, get_execution_timing(chosen["arn"]))
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
        verify, vt = verification_round(name, chosen["arn"], t_event - 60)
        assert verify["status"] == "SUCCEEDED", f"remediation did not converge: {verify['arn']}"
        metrics["t_verified"] = round(vt["stop"] - t_event, 2)
        confirmed = [v for v in query_violations(arn)
                     if v.get("round") == "verification" and v["state"] == "S_nominal"
                     and v.get("origin_execution") == chosen["arn"]]
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

        execs = wait_for_executions(name, t_event, ["SUCCEEDED"])
        assert execs, "No SUCCEEDED executions found"

        chosen = pick_execution(execs, event_name="PutBucketPublicAccessBlock")
        metrics["t_classification"] = get_execution_timing(chosen["arn"])["duration_s"]

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

        execs = wait_for_executions(name, ttl - 5, ["SUCCEEDED"], timeout=60)

        cfg = get_public_access(name)
        assert all(cfg.values()), f"Bucket not remediated: {cfg}"

        violations = query_violations(arn)
        states = [v["state"] for v in violations]
        assert "S_exception" in states, f"No S_exception: {states}"
        assert "S_violation" in states, f"No S_violation: {states}"

        # Timing: scheduler precision. A missing scheduler-triggered execution is a
        # failure, not a reason to skip the scenario's metrics.
        assert execs, "no finished execution after the exception expired"
        chosen = pick_execution(execs, trigger="exception_ttl_expiry")
        sched_exec = get_execution_timing(chosen["arn"])
        metrics["t_scheduler_fire"] = round(sched_exec["start"] - ttl, 2)
        metrics["t_remediation_post_expiry"] = round(sched_exec["stop"] - ttl, 2)
        metrics["t_remediation"] = sched_exec["duration_s"]
        verify, vt = verification_round(name, chosen["arn"], ttl - 5)
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
    """C4: Falha do sistema (bucket inexistente) → escalação → FAILED

    The bucket does not exist, so Analyze cannot read it: the control gets no
    state and escalates as a system failure. Nothing is remediated — the failed
    remediation path is C8.
    """
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

    reasons = escalation_reasons(exec_arn)
    assert reasons == ["system_failure"], f"escalation reasons: {reasons}"

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

        execs = wait_for_executions(name, t_event, ["SUCCEEDED"])
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
        chosen = pick_execution(execs, event_name="PutBucketPublicAccessBlock", longest=True)
        remediation = (chosen, get_execution_timing(chosen["arn"]))

        metrics["t_detection"] = round(remediation[1]["start"] - t_event, 2)
        metrics["t_remediation"] = remediation[1]["duration_s"]
        metrics["t_exposure"] = round(remediation[1]["stop"] - t_event, 2)

        cfg = get_public_access(name)
        assert all(cfg.values()), f"Bucket not remediated: {cfg}"

        verify, vt = verification_round(name, chosen["arn"], t_event - 60)
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

        execs = wait_for_executions(name, t_event, ["SUCCEEDED"])
        assert execs, "No SUCCEEDED executions found"

        chosen = pick_execution(execs, event_name="PutBucketPublicAccessBlock")
        metrics["t_classification"] = get_execution_timing(chosen["arn"])["duration_s"]

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

def test_c8():
    """C8: Remediação falha → S_violation preservado → escalação (remediation_failed)"""
    # C4 never reaches remediation: the bucket does not exist, so Analyze cannot
    # read it and the control escalates as a system failure, with no state. This
    # scenario covers the other escalation path — the resource is really in
    # S_violation, Analyze says so, and the remediation itself fails.
    #
    # The failure is induced, not mocked: a bucket policy denies exactly one
    # action, s3:PutBucketPublicAccessBlock, to exactly one principal — the role
    # the remediation runs as. Analyze (another role) still reads the bucket, and
    # this runner (an IAM user) can still break it and delete it.
    #
    # That principal is NOT the Lambda's own role: since the remediation assumes
    # a scoped session, the writes carry the remediation role. Denying the Lambda
    # role denies nothing, and the scenario silently stopped inducing a failure
    # when the session policy landed — the remediation simply succeeded.
    name = f"compliance-inttest-c8-{int(time.time())}"
    arn = f"arn:aws:s3:::{name}"
    metrics = {}
    role_arn = boto3.client("lambda", region_name=REGION).get_function_configuration(
        FunctionName=f"{PREFIX}-execute-{ENV}")["Environment"]["Variables"]["REMEDIATION_ROLE_ARN"]
    deny = {"Sid": "DenyRemediationForC8", "Effect": "Deny", "Principal": "*",
            "Action": "s3:PutBucketPublicAccessBlock", "Resource": arn,
            "Condition": {"ArnEquals": {"aws:PrincipalArn": role_arn}}}
    create_bucket(name)
    try:
        # Let the CreateBucket round finish first: it remediates ssl-only by
        # merging into the bucket policy, and a merge that read the policy before
        # the Deny was written would drop it.
        time.sleep(WAIT_CLOUDTRAIL)
        wait_quiesce()
        s3.put_bucket_policy(Bucket=name, Policy=json.dumps(
            {"Version": "2012-10-17", "Statement": [deny]}))
        # That write is a PutBucketPolicy event of its own: ssl-only merges its
        # statement back in, keeping the Deny. Wait for it and make sure.
        time.sleep(WAIT_CLOUDTRAIL)
        wait_quiesce()
        statements = json.loads(s3.get_bucket_policy(Bucket=name)["Policy"])["Statement"]
        assert any(st.get("Sid") == "DenyRemediationForC8" for st in statements), \
            "setup: the Deny statement did not survive the ssl-only merge"

        t_event = aws_now()
        set_public_access(name, block=False)
        log(f"Violation on {name}, remediation denied to the Execute role")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions(name, t_event, ["FAILED"])
        assert execs, "No FAILED execution found"
        chosen = pick_execution(execs, event_name="PutBucketPublicAccessBlock")
        desc = sfn.describe_execution(executionArn=chosen["arn"])
        assert desc["status"] == "FAILED" and desc["error"] == "ComplianceEscalation", \
            f"Expected FAILED/ComplianceEscalation, got {desc['status']}/{desc.get('error')}"

        # The escalation is the remediation-failure one, not a system failure.
        reasons = escalation_reasons(chosen["arn"])
        assert reasons == ["remediation_failed"], f"escalation reasons: {reasons}"
        metrics["t_escalation"] = get_execution_timing(chosen["arn"])["duration_s"]

        # The state stays S_violation, with the failure recorded next to it
        # (Property 24) — not a new state.
        failed = [v for v in query_violations(arn)
                  if v["state"] == "S_violation" and v.get("remediation_status") == "failed"]
        assert len(failed) == 1, f"expected one failed S_violation row, got {len(failed)}"
        assert "AccessDenied" in failed[0].get("remediation_error", ""), failed[0].get("remediation_error")

        # The resource is left as the violation left it, and a failed
        # remediation schedules no verification round.
        assert not any(get_public_access(name).values()), "bucket changed despite the failure"
        rounds = [x for x in executions_for(name, t_event - 5)
                  if x["input"].get("origin_execution") == chosen["arn"]]
        assert not rounds, f"{len(rounds)} verification round(s) for a failed remediation"

        # The issue is linked back to the violation row (Property 22). The link is
        # written by the escalation Lambda, asynchronously.
        deadline = time.time() + 90
        linked = None
        while time.time() < deadline and not linked:
            row = dynamodb.Table(VIOLATIONS_TABLE).get_item(
                Key={"violation_id": failed[0]["violation_id"]}).get("Item", {})
            linked = row.get("github_issue_url")
            if not linked:
                time.sleep(3)
        assert linked, "the GitHub issue was not linked to the violation row"

        log(f"t_escalation={metrics['t_escalation']}s  issue={linked}", "TIME")
        log("C8 PASSED", "OK")
        close_escalation_issue(name, "ctrl-s3-public-block")
        return True, metrics
    finally:
        delete_bucket(name)


def test_c9():
    """C9: Violação reincidente → disjuntor escala em vez de remediar de novo"""
    # The two rounds bound one incident. This is the other axis: an external
    # process that keeps re-applying the violating configuration. Each incident
    # converges, so nothing ever escalates — the loop and the other process just
    # take turns, and the conflict never reaches a human. The breaker counts the
    # recent successful remediations of this exact (resource, control) and
    # escalates instead of remediating past the threshold.
    name = f"compliance-inttest-c9-{int(time.time())}"
    arn = f"arn:aws:s3:::{name}"
    metrics = {}
    create_bucket(name)
    try:
        time.sleep(WAIT_CLOUDTRAIL)
        wait_quiesce()

        escalated = None
        for attempt in range(1, 6):
            t_event = aws_now()
            set_public_access(name, block=False)
            log(f"Violation {attempt} on {name}")
            time.sleep(WAIT_CLOUDTRAIL)
            execs = wait_for_executions(name, t_event, ["SUCCEEDED", "FAILED"])
            assert execs, f"attempt {attempt}: no finished execution"
            chosen = pick_execution(execs, event_name="PutBucketPublicAccessBlock", longest=True)
            if "recurrent_violation" in escalation_reasons(chosen["arn"]):
                escalated = (attempt, chosen)
                break
            assert all(get_public_access(name).values()), \
                f"attempt {attempt}: not remediated and not escalated"

        assert escalated, "the breaker never tripped in 5 repeated violations"
        attempt, chosen = escalated
        metrics["t_escalation"] = get_execution_timing(chosen["arn"])["duration_s"]
        metrics["violations_until_break"] = attempt

        # Escalated, not remediated: the resource is left as it is, and the row
        # carries no remediation_status at all (C8's carries "failed").
        assert not any(get_public_access(name).values()), "breaker escalated but something remediated"
        rows = [v for v in query_violations(arn)
                if v["state"] == "S_violation" and "remediation_status" not in v]
        assert rows, "no S_violation row without remediation for the escalated violation"
        assert sfn.describe_execution(executionArn=chosen["arn"])["error"] == "ComplianceEscalation"

        log(f"breaker tripped on violation {attempt}  t_escalation={metrics['t_escalation']}s", "TIME")
        log("C9 PASSED", "OK")
        close_escalation_issue(name, "ctrl-s3-public-block")
        return True, metrics
    finally:
        delete_bucket(name)


def test_c10():
    """C10: sessão de remediação escopada — um remediation.tf adulterado é barrado"""
    # The Execute holds no write permission of its own: it assumes the
    # remediation role with a session policy naming the resource that was
    # classified and the actions the control declared. Two things follow, and
    # this scenario exercises both by invoking the Lambda directly.
    #
    # 1. The remediation template is an artefact in S3. One that hardcodes
    #    another bucket — a tampered control, or a copy/paste mistake — is
    #    denied by IAM, because the session only reaches the classified resource.
    # 2. A control that under-declares its actions cannot borrow the role's
    #    other permissions: the session is the intersection.
    classified = f"compliance-inttest-c10a-{int(time.time())}"
    elsewhere = f"compliance-inttest-c10b-{int(time.time())}"
    rogue_key = f"controls/ctrl-test-rogue-{int(time.time())}/remediation.tf"
    metrics = {}
    lam = boto3.client("lambda", region_name=REGION)
    s3c = boto3.client("s3", region_name=REGION)
    bucket = os.environ.get("CONTROLS_BUCKET", "compliance-mvp-terraform-state")
    create_bucket(classified)
    create_bucket(elsewhere)
    try:
        time.sleep(WAIT_CLOUDTRAIL)
        wait_quiesce()
        # `elsewhere` stays compliant: the tampered template is what tries to
        # change it, and the loop would remediate it back if the test violated
        # it itself, hiding whether IAM refused.
        set_public_access(classified, block=False)
        time.sleep(WAIT_CLOUDTRAIL)
        wait_quiesce()

        rogue = f"""terraform {{
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }}
  }}
}}

provider "aws" {{
  region = var.region
}}

variable "bucket_name" {{ type = string }}
variable "region" {{ type = string }}

resource "aws_s3_bucket_public_access_block" "rogue" {{
  bucket                  = "{elsewhere}"
  block_public_acls       = false
  ignore_public_acls      = false
  block_public_policy     = false
  restrict_public_buckets = false
}}
"""
        s3c.put_object(Bucket=bucket, Key=rogue_key, Body=rogue.encode())

        def remediate(s3_key, actions):
            payload = {
                "resource_arn": f"arn:aws:s3:::{classified}",
                "resource_id": classified,
                "control_id": "ctrl-s3-public-block",
                "region": REGION,
                "violation_id": str(uuid.uuid4()),
                "remediation_s3_key": s3_key,
                "remediation_actions": actions,
                "remediation_scope": "resource",
            }
            resp = lam.invoke(FunctionName=f"{PREFIX}-execute-{ENV}", Payload=json.dumps(payload))
            return json.loads(resp["Payload"].read())

        full = ["s3:PutBucketPublicAccessBlock", "s3:GetBucketPublicAccessBlock"]

        tampered = remediate(rogue_key, full)
        assert tampered["remediation_status"] == "failed", \
            f"a template pointing at another bucket was applied: {str(tampered)[:300]}"
        assert "AccessDenied" in tampered.get("error", ""), tampered.get("error", "")[:200]
        assert all(get_public_access(elsewhere).values()), "denied, but the other bucket changed"
        log("remediation template pointing at another bucket was denied by IAM")

        under_declared = remediate("controls/ctrl-s3-public-block/remediation.tf",
                                   ["s3:GetBucketPublicAccessBlock"])
        assert under_declared["remediation_status"] == "failed", \
            f"the session reached an action the control never declared: {str(under_declared)[:300]}"
        assert "AccessDenied" in under_declared.get("error", ""), under_declared.get("error", "")[:200]
        log("action the control did not declare was denied by IAM")

        ok = remediate("controls/ctrl-s3-public-block/remediation.tf", full)
        assert ok["remediation_status"] == "success", str(ok)[:300]
        assert all(get_public_access(classified).values()), "scoped remediation did not apply"
        log("same remediation, correct scope, applied")
        log("C10 PASSED", "OK")
        return True, metrics
    finally:
        try:
            s3c.delete_object(Bucket=bucket, Key=rogue_key)
        except Exception as e:
            log(f"could not delete {rogue_key}: {e}", "WARN")
        delete_bucket(elsewhere)
        delete_bucket(classified)


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
        wait_for_executions(name, t_event, ["SUCCEEDED"])

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
        wait_for_executions(name, t_event, ["SUCCEEDED"])

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
        wait_for_executions(name, t_event, ["SUCCEEDED"])

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
        wait_for_executions(name, t_event, ["SUCCEEDED"])

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

def test_ctrl_ebs_encryption_default():
    """CTRL-05: ctrl-ebs-encryption-default re-enables the account setting"""
    # The only control outside S3, and the only account-level one: there is no
    # bucket or volume, the resource is the region's EBS-encryption-by-default
    # setting. Nothing billable is created — the EC2 calls are free — but the
    # change is account-wide, so the setting is put back if the loop does not.
    #
    # Coverage is preventive: the setting only affects volumes created later. An
    # existing unencrypted volume stays unencrypted and this control stays
    # compliant (design.md, L14).
    ec2 = boto3.client("ec2", region_name=REGION)
    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    # Account-level resources use the synthetic identifier ResolveResource builds
    # (design.md, L3) — that is the key the audit rows are written under.
    pseudo_arn = f"arn:aws:{REGION}:{account}"
    metrics = {}
    was_enabled = ec2.get_ebs_encryption_by_default()["EbsEncryptionByDefault"]
    try:
        t_event = aws_now()
        ec2.disable_ebs_encryption_by_default()
        log("EBS encryption by default disabled for the region")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions("DisableEbsEncryptionByDefault", t_event, ["SUCCEEDED"])
        assert execs, "No SUCCEEDED executions found"
        chosen = pick_execution(execs, event_name="DisableEbsEncryptionByDefault")
        metrics["t_remediation"] = get_execution_timing(chosen["arn"])["duration_s"]

        assert ec2.get_ebs_encryption_by_default()["EbsEncryptionByDefault"] is True, \
            "EBS encryption by default was not re-enabled"

        verify, vt = verification_round("account-level", chosen["arn"], t_event - 60)
        assert verify["status"] == "SUCCEEDED", f"remediation did not converge: {verify['arn']}"
        metrics["t_verified"] = round(vt["stop"] - t_event, 2)

        since = datetime.fromtimestamp(t_event - 5, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        rows = [v for v in query_violations(pseudo_arn)
                if v["control_id"] == "ctrl-ebs-encryption-default" and v["timestamp"] >= since]
        assert any(v["state"] == "S_violation" and v.get("remediation_status") == "success" for v in rows), \
            f"no remediated S_violation row under {pseudo_arn}"
        assert any(v["state"] == "S_nominal" and v.get("origin_execution") == chosen["arn"]
                   for v in rows), "verification left no S_nominal linked to the detection round"
        assert_no_remediation_echo("account-level", t_event - 5, {"EnableEbsEncryptionByDefault"})

        metrics["t_total"] = round(aws_now() - t_event, 2)
        log(f"EbsEncryptionByDefault = true  t_remediation={metrics['t_remediation']}s  "
            f"t_verified={metrics['t_verified']}s  t_total={metrics['t_total']}s", "TIME")
        log("CTRL-05 PASSED", "OK")
        return True, metrics
    finally:
        wait_quiesce()
        if was_enabled and not ec2.get_ebs_encryption_by_default()["EbsEncryptionByDefault"]:
            log("EBS encryption by default left disabled; restoring it", "WARN")
            ec2.enable_ebs_encryption_by_default()


def test_ctrl_ebs_volume_encrypted():
    """CTRL-06: volume sem criptografia → S_violation → escalação, sem remediação"""
    # A detective control: an existing volume cannot be encrypted in place (it is
    # snapshot -> copy -> new volume, with downtime and a new id), so the loop
    # classifies and escalates. control.json declares "remediation": "manual" and
    # PlanAction routes on it.
    #
    # Creating an unencrypted volume is only possible with encryption by default
    # off — which CTRL-05 remediates within seconds. The test therefore registers
    # an exception for ctrl-ebs-encryption-default over the account-level
    # pseudo-ARN: the loop classifies S_exception and leaves the setting alone.
    # That exercises the exception mechanism on an account-level resource too.
    ec2 = boto3.client("ec2", region_name=REGION)
    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    pseudo_arn = f"arn:aws:{REGION}:{account}"
    metrics = {}
    was_enabled = ec2.get_ebs_encryption_by_default()["EbsEncryptionByDefault"]
    eid = put_exception("ctrl-ebs-encryption-default", pseudo_arn, int(time.time()) + 900)
    volume_id = None
    try:
        time.sleep(2)
        ec2.disable_ebs_encryption_by_default()
        log("EBS encryption by default disabled under an approved exception")
        time.sleep(WAIT_CLOUDTRAIL)
        wait_quiesce()
        assert ec2.get_ebs_encryption_by_default()["EbsEncryptionByDefault"] is False, \
            "the exception did not hold: the loop re-enabled encryption by default"

        zone = ec2.describe_availability_zones()["AvailabilityZones"][0]["ZoneName"]
        t_event = aws_now()
        volume_id = ec2.create_volume(
            AvailabilityZone=zone, Size=1, VolumeType="gp3", Encrypted=False,
            TagSpecifications=[{"ResourceType": "volume",
                                "Tags": [{"Key": "Name", "Value": "compliance-inttest-ctrl06"}]}],
        )["VolumeId"]
        log(f"Unencrypted volume {volume_id} created")
        time.sleep(WAIT_CLOUDTRAIL)

        execs = wait_for_executions(volume_id, t_event, ["FAILED"])
        assert execs, "no finished execution for the volume"
        chosen = pick_execution(execs, event_name="CreateVolume")
        reasons = escalation_reasons(chosen["arn"])
        assert reasons == ["manual_remediation_required"], f"escalation reasons: {reasons}"
        metrics["t_escalation"] = get_execution_timing(chosen["arn"])["duration_s"]

        volume_arn = f"arn:aws:ec2:{REGION}:{account}:volume/{volume_id}"
        rows = [v for v in query_violations(volume_arn)
                if v["state"] == "S_violation" and "remediation_status" not in v]
        assert rows, f"no S_violation row under {volume_arn}"
        assert ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]["Encrypted"] is False, \
            "the volume was changed; a detective control must not remediate"

        log(f"escalated without remediation  t_escalation={metrics['t_escalation']}s", "TIME")
        log("CTRL-06 PASSED", "OK")
        close_escalation_issue(f"volume/{volume_id}", "ctrl-ebs-volume-encrypted")
        return True, metrics
    finally:
        wait_quiesce()
        if volume_id:
            try:
                ec2.get_waiter("volume_available").wait(VolumeIds=[volume_id])
                ec2.delete_volume(VolumeId=volume_id)
            except Exception as e:
                log(f"could not delete {volume_id}: {e}", "WARN")
        delete_exception(eid)
        wait_for_revocation_cascade()
        if was_enabled and not ec2.get_ebs_encryption_by_default()["EbsEncryptionByDefault"]:
            log("EBS encryption by default left disabled; restoring it", "WARN")
            ec2.enable_ebs_encryption_by_default()


TESTS = [
    ("C1", "Violação simples → remediação automática", test_c1),
    ("C2", "Exceção ativa → S_exception", test_c2),
    ("C3", "Exceção expira → remediação", test_c3),
    ("C4", "Falha do sistema (bucket inexistente) → escalação", test_c4),
    ("C5", "Violação por IaC → remediação", test_c5),
    ("C6", "Recurso já conforme → S_nominal", test_c6),
    ("C7", "Controls-sync → EventBridge dinâmico", test_c7),
    ("C8", "Remediação falha → escalação", test_c8),
    ("C9", "Violação reincidente → disjuntor", test_c9),
    ("C10", "Sessão de remediação escopada ao recurso", test_c10),
    ("CTRL-01", "ctrl-s3-public-block", test_ctrl_s3_public_block),
    ("CTRL-02", "ctrl-s3-ssl-only", test_ctrl_s3_ssl_only),
    ("CTRL-03", "ctrl-s3-versioning", test_ctrl_s3_versioning),
    ("CTRL-04", "ctrl-s3-encryption", test_ctrl_s3_encryption),
    ("CTRL-05", "ctrl-ebs-encryption-default", test_ctrl_ebs_encryption_default),
    ("CTRL-06", "ctrl-ebs-volume-encrypted (detectivo)", test_ctrl_ebs_volume_encrypted),
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
    # Same pooling as benchmark.py: H1/H3 are about the S3 violation scenarios.
    h1_tests = {k: v for k, v in all_metrics.items() if "t_remediation" in v and k in ("C1", "C3", "C5")}
    h3_tests = {k: v for k, v in all_metrics.items() if "t_exposure" in v and k in ("C1", "C5")}

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
    # These end FAILED by design: C4 (system failure), C8 (failed remediation),
    # C9 (the breaker refusing to remediate again) and CTRL-06 (detective control).
    expected_failed = sum(1 for n in ("C4", "C8", "C9", "CTRL-06") if n in results)
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
