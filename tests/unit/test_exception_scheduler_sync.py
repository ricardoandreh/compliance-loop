"""
Unit tests for the exception scheduler sync (DynamoDB Streams -> Scheduler).

This Lambda decides when an approved exception stops protecting a resource, and
every branch has a different consequence: schedule a re-evaluation for later,
re-evaluate now, or do nothing. Getting one wrong either leaves a resource
unprotected after its exception ended, or re-evaluates something twice. The
cases below are the branches, one per consequence:

- INSERT/MODIFY of a live grant       -> schedule at valid_until + 30 s
- the schedule already exists         -> update it instead of failing
- MODIFY to revoked                   -> cancel the schedule, re-evaluate now
- INSERT of a grant already expired   -> cancel the schedule, re-evaluate now
- REMOVE by DynamoDB's own TTL        -> nothing (the schedule already fired)
- REMOVE by a person, grant still live -> cancel, re-evaluate now
- REMOVE of a dead row (expired/revoked) -> cancel, do NOT re-evaluate
- a malformed row                     -> reported as a batch item failure,
                                         never swallowed
"""

import importlib.util
import json
import os
import sys
import time
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("EXCEPTIONS_TABLE", "exceptions")
os.environ.setdefault("STATE_MACHINE_ARN",
                      "arn:aws:states:us-east-1:123456789012:stateMachine:compliance-loop")
os.environ.setdefault("SCHEDULER_ROLE_ARN", "arn:aws:iam::123456789012:role/scheduler")

HANDLER = os.path.join(os.path.dirname(__file__), "../../lambdas/exception_scheduler_sync/handler.py")


@pytest.fixture
def h():
    spec = importlib.util.spec_from_file_location("exception_scheduler_sync_unit", HANDLER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["exception_scheduler_sync_unit"] = module
    spec.loader.exec_module(module)

    scheduler = MagicMock()
    scheduler.exceptions.ConflictException = type("ConflictException", (Exception,), {})
    scheduler.exceptions.ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
    module.scheduler = scheduler
    module.sfn = MagicMock()
    module.table = MagicMock()
    return module


NOW = int(time.time())
ARN = "arn:aws:s3:::my-bucket"


def grant(**overrides):
    item = {"exception_id": "exc-1", "control_id": "ctrl-s3-public-block",
            "resource_arn": ARN, "valid_until": NOW + 3600, "status": "active"}
    item.update(overrides)
    return item


def typed(item):
    """The low-level typed image a DynamoDB stream record carries."""
    out = {}
    for k, v in item.items():
        out[k] = {"N": str(v)} if isinstance(v, int) else {"S": v}
    return out


def record(event_name, item, seq="100", ttl_delete=False):
    rec = {"eventName": event_name, "dynamodb": {"SequenceNumber": seq}}
    if event_name == "REMOVE":
        rec["dynamodb"]["OldImage"] = typed(item)
    else:
        rec["dynamodb"]["NewImage"] = typed(item)
    if ttl_delete:
        rec["userIdentity"] = {"type": "Service", "principalId": "dynamodb.amazonaws.com"}
    return rec


def started_trigger(h):
    assert h.sfn.start_execution.call_count == 1
    return json.loads(h.sfn.start_execution.call_args.kwargs["input"])["trigger"]


def test_a_live_grant_is_scheduled_after_it_expires(h):
    h.table.get_item.return_value = {"Item": grant()}
    out = h.handler({"Records": [record("INSERT", grant())]}, None)

    assert out == {"batchItemFailures": []}
    kwargs = h.scheduler.create_schedule.call_args.kwargs
    assert kwargs["Name"] == "exc-exc-1"
    assert kwargs["ActionAfterCompletion"] == "DELETE"
    # 30 s after valid_until: the grant must be over when the loop looks again.
    fire = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW + 3600 + 30))
    assert kwargs["ScheduleExpression"] == f"at({fire})"
    target = json.loads(kwargs["Target"]["Input"])
    assert target["control_id"] == "ctrl-s3-public-block"
    assert target["trigger"] == "exception_ttl_expiry"
    h.sfn.start_execution.assert_not_called()


def test_an_existing_schedule_is_updated_not_duplicated(h):
    h.table.get_item.return_value = {"Item": grant()}
    h.scheduler.create_schedule.side_effect = h.scheduler.exceptions.ConflictException()
    out = h.handler({"Records": [record("MODIFY", grant(valid_until=NOW + 7200))]}, None)

    assert out == {"batchItemFailures": []}
    h.scheduler.update_schedule.assert_called_once()


def test_revoking_cancels_the_schedule_and_reevaluates_now(h):
    h.table.get_item.return_value = {"Item": grant(status="revoked")}
    h.handler({"Records": [record("MODIFY", grant(status="revoked"))]}, None)

    h.scheduler.delete_schedule.assert_called_once()
    assert started_trigger(h) == "exception_revoked"


def test_a_grant_already_expired_is_reevaluated_now(h):
    expired = grant(valid_until=NOW - 10)
    h.table.get_item.return_value = {"Item": expired}
    h.handler({"Records": [record("INSERT", expired)]}, None)

    h.scheduler.create_schedule.assert_not_called()
    h.scheduler.delete_schedule.assert_called_once()
    assert started_trigger(h) == "exception_expired"


def test_the_ttl_deletion_does_nothing(h):
    """The schedule created on INSERT already re-evaluated the resource; DynamoDB
    deleting the row hours later must not trigger a second, late evaluation."""
    h.handler({"Records": [record("REMOVE", grant(valid_until=NOW - 3600), ttl_delete=True)]}, None)

    h.scheduler.delete_schedule.assert_not_called()
    h.sfn.start_execution.assert_not_called()


def test_deleting_a_live_grant_by_hand_reevaluates_now(h):
    h.handler({"Records": [record("REMOVE", grant())]}, None)

    h.scheduler.delete_schedule.assert_called_once()
    assert started_trigger(h) == "exception_deleted"


@pytest.mark.parametrize("dead", [grant(valid_until=NOW - 60), grant(status="revoked")])
def test_deleting_a_dead_row_is_housekeeping(h, dead):
    """An expired or revoked row was already re-evaluated when it stopped being a
    grant; re-running now, maybe against a resource that no longer exists, only
    produces a spurious escalation."""
    h.handler({"Records": [record("REMOVE", dead)]}, None)

    h.scheduler.delete_schedule.assert_called_once()
    h.sfn.start_execution.assert_not_called()


def test_a_malformed_row_is_reported_not_swallowed(h):
    """Without control_id the re-evaluation cannot target the control. Raising
    turns it into a batch item failure, which the event source mapping retries
    and then sends to the stream DLQ, instead of a silent success."""
    broken = grant()
    del broken["control_id"]
    h.table.get_item.return_value = {"Item": broken}
    out = h.handler({"Records": [record("INSERT", broken, seq="777")]}, None)

    assert out == {"batchItemFailures": [{"itemIdentifier": "777"}]}


def test_a_row_deleted_before_processing_is_skipped(h):
    h.table.get_item.return_value = {}
    out = h.handler({"Records": [record("INSERT", grant())]}, None)

    assert out == {"batchItemFailures": []}
    h.scheduler.create_schedule.assert_not_called()


def test_a_missing_schedule_on_cancel_is_tolerated(h):
    h.scheduler.delete_schedule.side_effect = h.scheduler.exceptions.ResourceNotFoundException()
    out = h.handler({"Records": [record("REMOVE", grant())]}, None)

    assert out == {"batchItemFailures": []}
    assert started_trigger(h) == "exception_deleted"


def test_one_bad_record_does_not_block_the_rest(h):
    broken = grant(exception_id="exc-bad")
    del broken["control_id"]
    good = grant(exception_id="exc-good")
    h.table.get_item.side_effect = [{"Item": broken}, {"Item": good}]
    out = h.handler({"Records": [record("INSERT", broken, seq="1"),
                                 record("INSERT", good, seq="2")]}, None)

    assert out == {"batchItemFailures": [{"itemIdentifier": "1"}]}
    assert h.scheduler.create_schedule.call_args.kwargs["Name"] == "exc-exc-good"
