#!/usr/bin/env python3
"""
Start the executions the intake rule could not start.

When EventBridge matches a CloudTrail event but cannot start the state machine
— the role lost its permission, the state machine was being replaced, the
target stayed throttled for an hour — the event goes to the intake queue
instead of being dropped, and the intake-failed alarm fires. Once the cause is
fixed, this starts, for each queued message, the execution the rule should
have started.

The queue holds the rule's output, not the raw event: EventBridge dead-letters
the input its input transformer already built, with the refusal in the message
attributes (ERROR_CODE, ERROR_MESSAGE). Observed in the fault injection, where
the first version of this script expected the CloudTrail event and failed on
it. So the body is started as it is — there is no copy of the transformer here
to drift from the one in infra/modules/orchestration/main.tf.

Each message starts at most one execution. The execution is named after the
SQS message, so a replay interrupted between starting the execution and
deleting the message is resumed, not repeated: Step Functions refuses a second
execution with the same name (ExecutionAlreadyExists), and the message is then
deleted as already replayed.

Usage:
    python3 scripts/replay_intake.py            # replay everything queued
    python3 scripts/replay_intake.py --dry-run  # list it, start nothing
"""

import argparse
import json

import boto3

REGION = "us-east-1"
QUEUE = "compliance-loop-intake-failures-dev"
STATE_MACHINE = "compliance-loop-compliance-loop-dev"


def replay(session, dry_run=False):
    """Replay every queued message. Returns what was started, one dict per message."""
    sqs, sfn = session.client("sqs"), session.client("stepfunctions")
    account = session.client("sts").get_caller_identity()["Account"]
    machine = f"arn:aws:states:{REGION}:{account}:stateMachine:{STATE_MACHINE}"
    queue = sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]
    replayed = []
    while True:
        # A received message stays invisible for the visibility timeout, so one
        # pass drains the queue without seeing the same message twice. A dry
        # run hides it only briefly: the intake-failed alarm counts visible
        # messages, and a listing must not silence it.
        batch = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10, WaitTimeSeconds=2,
                                    VisibilityTimeout=10 if dry_run else 120,
                                    MessageAttributeNames=["All"]).get("Messages", [])
        if not batch:
            return replayed
        for message in batch:
            payload = json.loads(message["Body"])
            error = message.get("MessageAttributes", {}).get("ERROR_CODE", {}).get("StringValue")
            name = f"intake-replay-{message['MessageId']}"
            print(f"{payload.get('event_name')} at {payload.get('event_time')} (refused: {error}) -> {name}")
            if dry_run:
                continue
            try:
                sfn.start_execution(stateMachineArn=machine, name=name, input=message["Body"])
            except sfn.exceptions.ExecutionAlreadyExists:
                print("  already replayed; removing it from the queue")
            sqs.delete_message(QueueUrl=queue, ReceiptHandle=message["ReceiptHandle"])
            replayed.append({
                "execution_arn": machine.replace(":stateMachine:", ":execution:") + f":{name}",
                "event_name": payload.get("event_name"),
                "error_code": error,
            })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    replayed = replay(boto3.Session(region_name=REGION), dry_run=args.dry_run)
    print("dry run: nothing started" if args.dry_run else f"{len(replayed)} execution(s) started")


if __name__ == "__main__":
    main()
