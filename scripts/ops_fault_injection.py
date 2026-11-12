#!/usr/bin/env python3
"""
Inject faults and prove the operations layer reports them.

An alarm that has never fired is an assumption. Each scenario injects, on the
real account, one failure the loop cannot report as an escalation, and measures
how long each layer takes to report it — that the alarm published to the
alerts topic, not only changed state, and that the publication reached a
person: the GitHub issue the escalation Lambda opens for it, and the comment
its return to OK adds. Those issues are this run's, so they are closed at the
end; a run refuses to start while an operator's alarm issue is open.

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

# A run waits on the account for up to half an hour; a DNS blip in that window
# must not throw the measurement away (it did once). Standard retry mode
# retries connection errors, with backoff, which the legacy default gives up on
# after a few seconds.
os.environ.setdefault("AWS_RETRY_MODE", "standard")
os.environ.setdefault("AWS_MAX_ATTEMPTS", "10")

import boto3  # noqa: E402
from boto3.dynamodb.conditions import Key  # noqa: E402
from botocore.exceptions import ConnectionError as AWSConnectionError  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
import close_test_issues as gh  # noqa: E402
import replay_intake  # noqa: E402
import deployment  # noqa: E402

REGION = deployment.REGION
P = deployment.PREFIX_ENV
EXCEPTIONS = deployment.name("exceptions")
VIOLATIONS = deployment.name("violations-log")
STREAM_DLQ = deployment.name("stream-failures")
INTAKE_DLQ = replay_intake.QUEUE
INTAKE_ROLE = deployment.name("eventbridge-sfn")
DENY = "fault-injection-deny-start-execution"
TOPIC = deployment.name("alerts")
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
        try:
            current = states(cw, alarms)
        except AWSConnectionError as e:
            # Longer than the retries cover: keep waiting, the alarm will not.
            print(f"  network error while polling, still waiting: {e}")
            time.sleep(10)
            continue
        for name, state in current.items():
            if state == target and name not in reached:
                reached[name] = round(time.time() - since, 1)
                print(f"  {name}: {target} after {reached[name]} s")
        time.sleep(10)
    return {a: reached.get(a) for a in alarms}


def github(p, path, attempts=4):
    """GET from the GitHub API, riding out a transient network error."""
    for attempt in range(attempts):
        try:
            return gh.api(p, path)
        except OSError as e:
            if attempt == attempts - 1:
                raise
            print(f"  GitHub unreachable ({e}); retrying")
            time.sleep(5 * (attempt + 1))


def open_alarm_issue(p, alarm):
    base = f"/repos/{p['owner']}/{p['repo']}"
    return next((i for i in github(p, f"{base}/issues?state=open&labels=operations-alarm&per_page=100")
                 if i["title"] == f"[ALARM] {alarm}"), None)


def no_alarm_issue_open(alarms):
    p = gh.github_params(REGION)
    taken = [a for a in alarms if open_alarm_issue(p, a)]
    assert not taken, f"an operator's issue is open for {taken}; this run would comment on it and close it"


def notified(cw, alarms, since):
    """What each alarm did since `since`, beyond changing state.

    - actions: the executed actions from the alarm history — it published.
    - issue: the GitHub issue it opened (topic -> queue -> escalation Lambda),
      how long after the first action it existed, and whether the return to
      OK was commented on. Closed afterwards: it belongs to this run.
    """
    p = gh.github_params(REGION)
    base = f"/repos/{p['owner']}/{p['repo']}"
    start = datetime.datetime.fromtimestamp(since, datetime.timezone.utc)
    out = {}
    for a in alarms:
        items = sorted(cw.describe_alarm_history(AlarmName=a, HistoryItemType="Action",
                                                 StartDate=start)["AlarmHistoryItems"],
                       key=lambda i: i["Timestamp"])
        entry = {"actions": [f"{i['Timestamp'].astimezone(datetime.timezone.utc):%H:%M:%S}Z "
                             f"{i['HistorySummary']}" for i in items],
                 "issue": None, "seconds_from_alarm_to_issue": None, "ok_commented": False}
        issue = open_alarm_issue(p, a)
        if issue:
            deadline = time.time() + 120
            while time.time() < deadline and not entry["ok_commented"]:
                comments = github(p, f"{base}/issues/{issue['number']}/comments")
                entry["ok_commented"] = any("Voltou a OK" in c["body"] for c in comments)
                if not entry["ok_commented"]:
                    time.sleep(5)
            created = datetime.datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
            entry["issue"] = issue["number"]
            if items:
                entry["seconds_from_alarm_to_issue"] = round((created - items[0]["Timestamp"]).total_seconds(), 1)
            gh.close_issue(p, issue["number"])
        out[a] = entry
    return out


def reached_a_person(result):
    return all(n["issue"] and n["ok_commented"] for n in result["notified"].values())


def depth(sqs, queue):
    attrs = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=[
        "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])


def sync(s):
    sqs, cw = s.client("sqs"), s.client("cloudwatch")
    # Only the exception-writer role may write the table (its resource policy
    # denies everyone else), so the bad row goes in the way a real one would.
    account = s.client("sts").get_caller_identity()["Account"]
    creds = s.client("sts").assume_role(RoleArn=f"arn:aws:iam::{account}:role/{deployment.name('exception-writer')}",
                                        RoleSessionName="fault-injection")["Credentials"]
    ddb = boto3.Session(region_name=REGION, aws_access_key_id=creds["AccessKeyId"],
                        aws_secret_access_key=creds["SecretAccessKey"],
                        aws_session_token=creds["SessionToken"]).resource("dynamodb").Table(EXCEPTIONS)
    alarms = [f"{P}-exception-sync-failures", f"{P}-stream-dlq"]
    queue = sqs.get_queue_url(QueueName=STREAM_DLQ)["QueueUrl"]

    before = states(cw, alarms)
    assert all(v != "ALARM" for v in before.values()), f"an alarm is already firing: {before}"
    no_alarm_issue_open(alarms)
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

    result = {
        "injected": exception_id,
        "seconds_to_alarm": to_alarm,
        "stream_dlq_messages": in_dlq,
        "dlq_record_context": context,
        "seconds_to_ok_after_clearing": to_ok,
        "notified": notified(cw, alarms, t0),
    }
    result["passed"] = all(to_alarm.values()) and all(to_ok.values()) and in_dlq > 0 and reached_a_person(result)
    return result


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
    no_alarm_issue_open([alarm])
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
        kw = {} if REGION == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
        s3.create_bucket(Bucket=bucket, **kw)
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
                        and bool(result["seconds_to_ok_after_replay"]) and reached_a_person(result))
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
