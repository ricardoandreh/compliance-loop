#!/usr/bin/env python3
"""
Inject one fault and prove the operations layer reports it.

An alarm that has never fired is an assumption. This injects the failure C3
exposed — an exception change the sync Lambda cannot turn into a schedule — and
measures, on the real account, how long each layer takes to report it:

1. the per-record failure is logged, and the log metric filter turns it into
   the exception-sync-failures alarm;
2. after the bounded retries, the stream record lands in the stream DLQ instead
   of being dropped, and the stream-dlq alarm fires;
3. both return to OK once the fault is cleared.

The injected row has no control_id (the handler refuses it by design) and a
valid_until already in the past, so that deleting it afterwards is plain
housekeeping and does not fail a second time. Nothing else in the account is
touched: no bucket, no policy, no state machine.

Usage:
    python3 scripts/ops_fault_injection.py
"""

import datetime
import json
import time
import uuid

import boto3

REGION = "us-east-1"
P = "compliance-loop-dev"
EXCEPTIONS = "compliance-loop-exceptions-dev"
DLQ = "compliance-loop-stream-failures-dev"
ALARMS = [f"{P}-exception-sync-failures", f"{P}-stream-dlq"]


def states(cw):
    r = cw.describe_alarms(AlarmNames=ALARMS)
    return {a["AlarmName"]: a["StateValue"] for a in r["MetricAlarms"]}


def wait_for(cw, target, timeout, since):
    """Seconds from `since` until each alarm reached `target` (None = never)."""
    reached = {}
    deadline = time.time() + timeout
    while time.time() < deadline and len(reached) < len(ALARMS):
        for name, state in states(cw).items():
            if state == target and name not in reached:
                reached[name] = round(time.time() - since, 1)
                print(f"  {name}: {target} after {reached[name]} s")
        time.sleep(10)
    return {a: reached.get(a) for a in ALARMS}


def main():
    s = boto3.Session(region_name=REGION)
    ddb, sqs, cw = s.resource("dynamodb").Table(EXCEPTIONS), s.client("sqs"), s.client("cloudwatch")
    queue = sqs.get_queue_url(QueueName=DLQ)["QueueUrl"]

    before = states(cw)
    print("alarms before:", before)
    assert all(v != "ALARM" for v in before.values()), "an alarm is already firing; clear it first"

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
    print(f"injected {exception_id} at {datetime.datetime.now(datetime.timezone.utc):%H:%M:%S}Z")

    to_alarm = wait_for(cw, "ALARM", timeout=900, since=t0)

    attrs = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["ApproximateNumberOfMessages"])
    in_dlq = int(attrs["Attributes"]["ApproximateNumberOfMessages"])
    sample = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=1, VisibilityTimeout=0)
    context = None
    if sample.get("Messages"):
        body = json.loads(sample["Messages"][0]["Body"])
        context = {k: body.get("requestContext", {}).get(k) for k in ("condition", "approximateInvokeCount")}

    # Clear the fault: remove the row, empty the DLQ, wait for the alarms to recover.
    ddb.delete_item(Key={"exception_id": exception_id})
    sqs.purge_queue(QueueUrl=queue)
    t1 = time.time()
    to_ok = wait_for(cw, "OK", timeout=900, since=t1)

    result = {
        "injected": exception_id,
        "seconds_to_alarm": to_alarm,
        "stream_dlq_messages": in_dlq,
        "dlq_record_context": context,
        "seconds_to_ok_after_clearing": to_ok,
    }
    print(json.dumps(result, indent=2))
    with open("results/ops_fault_injection.json", "w") as f:
        json.dump(result, f, indent=2)
    print("wrote results/ops_fault_injection.json")
    return 0 if all(to_alarm.values()) and in_dlq > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
