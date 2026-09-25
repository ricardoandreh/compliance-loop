"""
Replaying the intake queue must start each lost detection exactly once.

The queue holds the input the rule's transformer built (observed in the fault
injection), so the replay starts it verbatim. What can go wrong is the
bookkeeping around it: a replay interrupted after starting the execution but
before deleting the message must not start a second one, and a start that
fails must leave the message queued for the next run.
"""

import importlib.util
import json
import os
from unittest.mock import MagicMock

import pytest

_spec = importlib.util.spec_from_file_location(
    "replay_intake", os.path.join(os.path.dirname(__file__), "../../scripts/replay_intake.py"))
replay_intake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay_intake)

BODY = json.dumps({"account_id": "123456789012", "region": "us-east-1",
                   "event_time": "2026-09-25T02:04:54Z", "event_name": "CreateBucket",
                   "request_parameters": {"bucketName": "b"}, "response_elements": None})


class AlreadyExists(Exception):
    pass


def session(messages, start_error=None):
    sqs, sfn, sts = MagicMock(), MagicMock(), MagicMock()
    sqs.get_queue_url.return_value = {"QueueUrl": "q"}
    sqs.receive_message.side_effect = [{"Messages": messages}, {"Messages": []}]
    sfn.exceptions.ExecutionAlreadyExists = AlreadyExists
    if start_error:
        sfn.start_execution.side_effect = start_error
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    s = MagicMock()
    s.client.side_effect = {"sqs": sqs, "stepfunctions": sfn, "sts": sts}.__getitem__
    return s, sqs, sfn


def message(mid="m-1"):
    return {"MessageId": mid, "ReceiptHandle": f"rh-{mid}", "Body": BODY,
            "MessageAttributes": {"ERROR_CODE": {"StringValue": "NO_PERMISSIONS"}}}


def test_the_queued_input_is_started_verbatim_and_then_deleted():
    s, sqs, sfn = session([message()])
    replayed = replay_intake.replay(s)
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["input"] == BODY
    assert kwargs["name"] == "intake-replay-m-1"
    sqs.delete_message.assert_called_once_with(QueueUrl="q", ReceiptHandle="rh-m-1")
    assert replayed[0]["error_code"] == "NO_PERMISSIONS"


def test_an_interrupted_replay_is_resumed_not_repeated():
    """Same name as an execution that already ran: Step Functions refuses it,
    and the message is only removed."""
    s, sqs, sfn = session([message()], start_error=AlreadyExists())
    replay_intake.replay(s)
    sqs.delete_message.assert_called_once()


def test_a_start_that_fails_leaves_the_message_queued():
    s, sqs, sfn = session([message()], start_error=RuntimeError("AccessDenied"))
    with pytest.raises(RuntimeError):
        replay_intake.replay(s)
    sqs.delete_message.assert_not_called()


def test_dry_run_starts_nothing():
    s, sqs, sfn = session([message()])
    replay_intake.replay(s, dry_run=True)
    sfn.start_execution.assert_not_called()
    sqs.delete_message.assert_not_called()
