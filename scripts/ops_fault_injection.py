#!/usr/bin/env python3
"""
Inject faults and prove the operations layer reports them.

An alarm that has never fired is an assumption. Each scenario injects, on the
real account, one failure the loop cannot report as an escalation, and measures
how long each layer takes to report it — and that the alarm published to the
alerts topic, not only changed state.

sync — an exception change the sync Lambda cannot turn into a schedule (the
    failure C3 exposed): a row with no control_id and a valid_until already
    past. The per-record failure is logged and becomes the
    exception-sync-failures alarm; after the bounded retries the stream record
    lands in the stream DLQ and stream-dlq fires. Deleting the row afterwards
    is housekeeping (the grant is already dead), so the fault does not recur.

intake — a detection EventBridge cannot deliver. An explicit Deny on
    states:StartExecution is attached to the intake role and a bucket is
    created: the CreateBucket event matches the rule, the start is refused, and
    the event must land in the intake queue instead of being dropped, firing
    intake-failed. The Deny is removed in a finally block, the queued event is
    replayed with scripts/replay_intake.py, and the replayed execution must
    evaluate the bucket as the lost one would have. The bucket is deleted at
    the end.

Nothing else is touched. Never run this concurrently with the integration
suite: while the Deny is attached, every detection in the account is refused.

Usage:
    python3 scripts/ops_fault_injection.py                  # both scenarios
    python3 scripts/ops_fault_injection.py --scenario intake
"""

import argparse
import datetime
import json
import os
import sys
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, os.path.dirname(__file__))
import replay_intake  # noqa: E402

REGION = "us-east-1"
P = "compliance-loop-dev"
EXCEPTIONS = "compliance-loop-exceptions-dev"
VIOLATIONS = "compliance-loop-violations-log-dev"
STREAM_DLQ = "compliance-loop-stream-failures-dev"
INTAKE_DLQ = replay_intake.QUEUE
INTAKE_ROLE = "compliance-loop-eventbridge-sfn-dev"
DENY = "fault-injection-deny-start-execution"
TOPIC = "compliance-loop-alerts-dev"
RESULTS = "results/ops_fault_injection.json"
IAM_PROPAGATION = 30


def now():
    return f"{datetime.datetime.now(datetime.timezone.utc):%H:%M:%S}Z"


def states(cw, alarms):
    r = cw.describe_alarms(AlarmNames=alarms)
    return {a["AlarmName"]: a["StateValue"] for a in r["MetricAlarms"]}


def wait_for(cw, alarms, target, timeout, since):
    """Seconds from `since` until each alarm reached `target` (None = never)."""
    reached = {}
    deadline = time.time() + timeout
    while time.time() < deadline and len(reached) < len(alarms):
        for name, state in states(cw, alarms).items():
            if state == target and name not in reached:
                reached[name] = round(time.time() - since, 1)
                print(f"  {name}: {target} after {reached[name]} s")
        time.sleep(10)
    return {a: reached.get(a) for a in alarms}


def notified(cw, alarms, since):
    """The actions each alarm executed since `since`: proof it published, not
    only changed state."""
    start = datetime.datetime.fromtimestamp(since, datetime.timezone.utc)
    out = {}
    for a in alarms:
        items = cw.describe_alarm_history(AlarmName=a, HistoryItemType="Action", StartDate=start)
        out[a] = [f"{i['Timestamp'].astimezone(datetime.timezone.utc):%H:%M:%S}Z {i['HistorySummary']}"
                  for i in sorted(items["AlarmHistoryItems"], key=lambda i: i["Timestamp"])]
    return out


def depth(sqs, queue):
    attrs = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=[
        "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])


def sync(s):
    ddb, sqs, cw = s.resource("dynamodb").Table(EXCEPTIONS), s.client("sqs"), s.client("cloudwatch")
    alarms = [f"{P}-exception-sync-failures", f"{P}-stream-dlq"]
    queue = sqs.get_queue_url(QueueName=STREAM_DLQ)["QueueUrl"]

    before = states(cw, alarms)
    assert all(v != "ALARM" for v in before.values()), f"an alarm is already firing: {before}"
    # The queue is purged at the end; it must hold nothing but what this run puts there.
    assert depth(sqs, queue) == 0, "the stream DLQ holds real failures; handle them first"

    exception_id = f"fault-injection-{uuid.uuid4().hex[:8]}"
    t0 = time.time()
    ddb.put_item(Item={
        "exception_id": exception_id,
        # control_id deliberately missing: the sync refuses the row.
        "resource_arn": "arn:aws:s3:::fault-injection-not-a-bucket",
        "valid_until": int(t0) - 60,
        "status": "active",
        "justification": "operations fault injection (scripts/ops_fault_injection.py)",
        "approved_by": "fault-injection",
    })
    print(f"injected {exception_id} at {now()}")

    to_alarm = wait_for(cw, alarms, "ALARM", timeout=900, since=t0)

    in_dlq = depth(sqs, queue)
    sample = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=1, VisibilityTimeout=0)
    context = None
    if sample.get("Messages"):
        body = json.loads(sample["Messages"][0]["Body"])
        context = {k: body.get("requestContext", {}).get(k) for k in ("condition", "approximateInvokeCount")}

    # Clear the fault: remove the row, empty the DLQ, wait for the alarms to recover.
    ddb.delete_item(Key={"exception_id": exception_id})
    sqs.purge_queue(QueueUrl=queue)
    t1 = time.time()
    to_ok = wait_for(cw, alarms, "OK", timeout=900, since=t1)

    return {
        "injected": exception_id,
        "seconds_to_alarm": to_alarm,
        "stream_dlq_messages": in_dlq,
        "dlq_record_context": context,
        "seconds_to_ok_after_clearing": to_ok,
        "notified": notified(cw, alarms, t0),
        "passed": all(to_alarm.values()) and all(to_ok.values()) and in_dlq > 0,
    }


def executions_about(sfn, machine, needle, since):
    """(arn, status, input) of the executions started after `since` whose input names `needle`."""
    found = []
    for page in sfn.get_paginator("list_executions").paginate(
            stateMachineArn=machine, PaginationConfig={"MaxItems": 100}):
        for e in page["executions"]:
            if e["startDate"].timestamp() < since:
                return found
            raw = sfn.describe_execution(executionArn=e["executionArn"]).get("input") or "{}"
            if needle in raw:
                found.append((e["executionArn"], e["status"], json.loads(raw)))
    return found


def wait_until_done(sfn, arn, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = sfn.describe_execution(executionArn=arn)["status"]
        if status != "RUNNING":
            return status
        time.sleep(3)
    return "RUNNING"


def intake(s):
    iam, s3, sqs, cw, sfn = (s.client(c) for c in ("iam", "s3", "sqs", "cloudwatch", "stepfunctions"))
    log = s.resource("dynamodb").Table(VIOLATIONS)
    alarm = f"{P}-intake-failed"
    account = s.client("sts").get_caller_identity()["Account"]
    machine = f"arn:aws:states:{REGION}:{account}:stateMachine:{replay_intake.STATE_MACHINE}"
    queue = sqs.get_queue_url(QueueName=INTAKE_DLQ)["QueueUrl"]

    # A Deny left behind by an interrupted run would block every detection, and
    # Terraform would not notice it: it does not own the role's inline policies.
    if DENY in iam.list_role_policies(RoleName=INTAKE_ROLE)["PolicyNames"]:
        iam.delete_role_policy(RoleName=INTAKE_ROLE, PolicyName=DENY)
        print("removed a Deny left by an earlier run")
    assert states(cw, [alarm])[alarm] != "ALARM", "intake-failed is already firing; clear it first"
    assert depth(sqs, queue) == 0, "the intake queue holds real detections; replay them first"

    bucket = f"compliance-fault-intake-{uuid.uuid4().hex[:8]}"
    result = {"bucket": bucket}
    t0 = time.time()
    try:
        iam.put_role_policy(RoleName=INTAKE_ROLE, PolicyName=DENY, PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Deny", "Action": "states:StartExecution", "Resource": "*"}],
        }))
        print(f"Deny attached to {INTAKE_ROLE} at {now()}; {IAM_PROPAGATION} s for IAM to propagate")
        time.sleep(IAM_PROPAGATION)

        t0 = time.time()
        s3.create_bucket(Bucket=bucket)
        print(f"created {bucket} at {now()}")
        deadline = t0 + 300
        while time.time() < deadline and depth(sqs, queue) == 0:
            # The Deny must be what stops the event: an execution for the bucket
            # means IAM had not propagated, and the scenario proves nothing.
            assert not executions_about(sfn, machine, bucket, t0), \
                "an execution started despite the Deny (IAM not yet propagated); rerun"
            time.sleep(5)
        result["seconds_to_queue"] = round(time.time() - t0, 1) if depth(sqs, queue) else None
        print(f"  event in the intake queue after {result['seconds_to_queue']} s")
    finally:
        iam.delete_role_policy(RoleName=INTAKE_ROLE, PolicyName=DENY)
        print(f"Deny removed at {now()}")

    assert result["seconds_to_queue"] is not None, "the event never reached the intake queue"
    result["executions_before_replay"] = len(executions_about(sfn, machine, bucket, t0))
    # What EventBridge queued: the input its transformer built (not the raw
    # CloudTrail event), with the refusal in the attributes. Retry attributes
    # appear only when the failure was retried.
    peek = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=1, VisibilityTimeout=0,
                               MessageAttributeNames=["All"])["Messages"][0]
    body = json.loads(peek["Body"])
    result["queued"] = {
        "body_keys": sorted(body),
        "event_name": body.get("event_name"),
        **{k: v["StringValue"] for k, v in peek.get("MessageAttributes", {}).items()
           if k in ("ERROR_CODE", "EXHAUSTED_RETRY_CONDITION", "RETRY_ATTEMPTS")},
    }

    result["seconds_to_alarm"] = wait_for(cw, [alarm], "ALARM", timeout=900, since=t0)[alarm]

    t1 = time.time()
    result["replayed"] = len(replay_intake.replay(s))
    origin = next(arn for arn, _, _ in executions_about(sfn, machine, bucket, t1) if ":intake-replay-" in arn)
    result["replay_status"] = wait_until_done(sfn, origin)
    # The replayed detection remediates a fresh bucket's defaults (no
    # versioning, no TLS-only policy), so it schedules one verification round.
    deadline = time.time() + 120
    rounds = []
    while time.time() < deadline:
        rounds = [x for x in executions_about(sfn, machine, bucket, t1)
                  if x[2].get("mode") == "verify" and x[2].get("origin_execution") == origin]
        if rounds and rounds[0][1] != "RUNNING":
            break
        time.sleep(5)
    result["verification_status"] = rounds[0][1] if rounds else None

    rows = log.query(IndexName="resource_arn-timestamp-index",
                     KeyConditionExpression=Key("resource_arn").eq(f"arn:aws:s3:::{bucket}"))["Items"]
    result["audit_rows"] = sorted(
        f"{r['control_id']} {r.get('round', 'detection')} {r['state']} {r.get('remediation_status', '-')}"
        for r in rows)

    result["seconds_to_ok_after_replay"] = wait_for(cw, [alarm], "OK", timeout=900, since=t1)[alarm]
    result["notified"] = notified(cw, [alarm], t0)

    deadline = time.time() + 120
    while time.time() < deadline and sfn.list_executions(
            stateMachineArn=machine, statusFilter="RUNNING", maxResults=1)["executions"]:
        time.sleep(3)
    s3.delete_bucket(Bucket=bucket)
    result["passed"] = (result["executions_before_replay"] == 0 and bool(result["seconds_to_alarm"])
                        and result["replay_status"] == "SUCCEEDED"
                        and result["verification_status"] == "SUCCEEDED"
                        and bool(result["seconds_to_ok_after_replay"]))
    return result


SCENARIOS = {"sync": sync, "intake": intake}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), action="append")
    args = ap.parse_args()
    s = boto3.Session(region_name=REGION)

    try:
        with open(RESULTS) as f:
            results = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        results = {}
    if "scenarios" not in results:
        results = {"scenarios": {}}
    topic = next(t["TopicArn"] for t in s.client("sns").list_topics()["Topics"]
                 if t["TopicArn"].endswith(f":{TOPIC}"))
    # Alarms publish to the topic; whether a person receives it depends on who
    # subscribed — recorded so the result does not claim more than delivery to SNS.
    results["topic_subscriptions"] = len(
        s.client("sns").list_subscriptions_by_topic(TopicArn=topic)["Subscriptions"])

    for name in args.scenario or ["sync", "intake"]:
        print(f"== {name} ==")
        results["scenarios"][name] = SCENARIOS[name](s)
        print(json.dumps(results["scenarios"][name], indent=2, default=str))
        with open(RESULTS, "w") as f:
            json.dump(results, f, indent=2, default=str)
    print(f"wrote {RESULTS}")
    return 0 if all(r.get("passed") for r in results["scenarios"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
