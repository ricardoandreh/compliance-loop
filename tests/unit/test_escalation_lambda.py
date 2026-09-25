"""
Unit tests for the escalation Lambda's issue deduplication.

The point under test is which source decides that an issue already exists. It
used to be the GitHub Search API, whose index lags by seconds to minutes: a
just-opened issue was invisible and a duplicate was created for the same
resource and control (design.md, L5). The audit trail answers the same question
without a delay, and the issue's own state is read directly.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("VIOLATIONS_TABLE", "violations_log")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

HANDLER = os.path.join(os.path.dirname(__file__), "../../lambdas/escalation/handler.py")


@pytest.fixture(scope="module")
def handler():
    spec = importlib.util.spec_from_file_location("escalation_handler", HANDLER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["escalation_handler"] = module
    spec.loader.exec_module(module)
    return module


PARAMS = {"pat": "t", "owner": "o", "repo": "r"}


def rows(*items):
    return {"Items": list(items)}


def test_links_to_the_issue_the_audit_trail_names(handler):
    table = MagicMock()
    table.query.return_value = rows({"github_issue_id": "42", "control_id": "ctrl-x"})
    with patch.object(handler, "violations_table", table), \
         patch.object(handler, "issue_is_open", return_value=True):
        assert handler.open_issue_from_log("arn:aws:s3:::b", "ctrl-x", **PARAMS) == 42
    assert table.query.call_args.kwargs["IndexName"] == "resource_arn-timestamp-index"
    # Most recent first: the issue that tracks the resource now, not the first ever.
    assert table.query.call_args.kwargs["ScanIndexForward"] is False


def test_skips_issues_that_were_closed(handler):
    table = MagicMock()
    table.query.return_value = rows({"github_issue_id": "7"}, {"github_issue_id": "3"})
    with patch.object(handler, "violations_table", table), \
         patch.object(handler, "issue_is_open", side_effect=[False, True]):
        assert handler.open_issue_from_log("arn:aws:s3:::b", "ctrl-x", **PARAMS) == 3


def test_no_issue_in_the_trail_yields_none(handler):
    table = MagicMock()
    table.query.return_value = rows()
    with patch.object(handler, "violations_table", table):
        assert handler.open_issue_from_log("arn:aws:s3:::b", "ctrl-x", **PARAMS) is None


def test_no_limit_is_applied_before_the_filter(handler):
    """DynamoDB applies Limit to the rows it reads, before the FilterExpression:
    a limit would let other controls' rows crowd out the one being looked for."""
    table = MagicMock()
    table.query.return_value = rows()
    with patch.object(handler, "violations_table", table):
        handler.open_issue_from_log("arn:aws:s3:::b", "ctrl-x", **PARAMS)
    assert "Limit" not in table.query.call_args.kwargs


def test_an_issue_on_a_later_page_is_found(handler):
    """The first page can come back empty after the filter and still carry a
    LastEvaluatedKey: the lookup has to keep paging, not stop there."""
    table = MagicMock()
    table.query.side_effect = [
        {"Items": [], "LastEvaluatedKey": {"k": "1"}},
        {"Items": [{"github_issue_id": "42"}]},
    ]
    with patch.object(handler, "violations_table", table), \
         patch.object(handler, "issue_is_open", return_value=True):
        assert handler.open_issue_from_log("arn:aws:s3:::b", "ctrl-x", **PARAMS) == 42
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": "1"}


def test_the_same_issue_is_checked_once(handler):
    """Several rows of one incident point at the same issue; it is one GitHub
    call, not one per row."""
    table = MagicMock()
    table.query.return_value = rows({"github_issue_id": "7"}, {"github_issue_id": "7"},
                                    {"github_issue_id": "7"})
    with patch.object(handler, "violations_table", table), \
         patch.object(handler, "issue_is_open", return_value=False) as is_open:
        assert handler.open_issue_from_log("arn:aws:s3:::b", "ctrl-x", **PARAMS) is None
    is_open.assert_called_once()


def test_search_api_is_only_the_fallback(handler):
    """A system failure has a synthetic violation_id and no row of its own, so
    the trail cannot answer — only then is the delayed index consulted."""
    record = {"messageId": "m1", "body": json.dumps({
        "violation_id": "analyze-failed-x", "resource_arn": "arn:aws:s3:::b",
        "control_id": "ctrl-x", "error": "boom", "timestamp": "2026-09-20T00:00:00Z",
        "step_functions_execution_arn": "arn:exec", "account_id": "1", "region": "us-east-1"})}

    with patch.object(handler, "get_github_params", return_value=PARAMS), \
         patch.object(handler, "open_issue_from_log", return_value=None) as from_log, \
         patch.object(handler, "find_open_issue", return_value=None) as search, \
         patch.object(handler, "create_github_issue",
                      return_value={"number": 9, "html_url": "u"}) as create, \
         patch.object(handler, "link_issue") as link:
        out = handler.lambda_handler({"Records": [record]}, None)

    assert out == {"batchItemFailures": []}
    from_log.assert_called_once()
    search.assert_called_once()
    create.assert_called_once()
    link.assert_called_once_with("analyze-failed-x", 9, "u")


def test_an_issue_in_the_trail_skips_both_the_search_and_a_new_issue(handler):
    record = {"messageId": "m1", "body": json.dumps({
        "violation_id": "v-2", "resource_arn": "arn:aws:s3:::b", "control_id": "ctrl-x",
        "error": "boom", "timestamp": "2026-09-20T00:00:00Z",
        "step_functions_execution_arn": "arn:exec", "account_id": "1", "region": "us-east-1"})}

    with patch.object(handler, "get_github_params", return_value=PARAMS), \
         patch.object(handler, "open_issue_from_log", return_value=42), \
         patch.object(handler, "find_open_issue") as search, \
         patch.object(handler, "create_github_issue") as create, \
         patch.object(handler, "link_issue") as link:
        handler.lambda_handler({"Records": [record]}, None)

    search.assert_not_called()
    create.assert_not_called()
    link.assert_called_once_with("v-2", 42, "https://github.com/o/r/issues/42")


# --- Alarm transitions -------------------------------------------------------
# The alerts topic delivers CloudWatch alarm transitions to their own queue,
# which feeds this same function: an alarm reaches a person as an issue, like an
# escalation does.

ALARM = {
    "AlarmName": "compliance-loop-dev-intake-failed",
    "AlarmDescription": "A detection did not start an execution. Runbook: design.md, Operação.",
    "AWSAccountId": "123456789012",
    "NewStateValue": "ALARM",
    "NewStateReason": "Threshold Crossed: 1 datapoint [1.0] was greater than the threshold (0.0).",
    "StateChangeTime": "2026-09-25T02:19:58.000+0000",
    "Region": "US East (N. Virginia)",
    "AlarmArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:compliance-loop-dev-intake-failed",
    "OldStateValue": "OK",
}


def transition(state):
    return {"messageId": "a1", "body": json.dumps({**ALARM, "NewStateValue": state})}


def run_alarm(handler, record, open_issue):
    with patch.object(handler, "get_github_params", return_value=PARAMS), \
         patch.object(handler, "open_alarm_issue", return_value=open_issue) as lookup, \
         patch.object(handler, "create_github_issue",
                      return_value={"number": 77, "html_url": "u"}) as create, \
         patch.object(handler, "comment_on_issue") as comment, \
         patch.object(handler, "open_issue_from_log") as violation_dedup:
        out = handler.lambda_handler({"Records": [record]}, None)
    violation_dedup.assert_not_called()
    return out, lookup, create, comment


def test_an_alarm_opens_an_issue_under_its_own_label(handler):
    out, lookup, create, comment = run_alarm(handler, transition("ALARM"), open_issue=None)
    assert out == {"batchItemFailures": []}
    lookup.assert_called_once_with("t", "o", "r", "[ALARM] compliance-loop-dev-intake-failed")
    title, body = create.call_args.args[3], create.call_args.args[4]
    assert title == "[ALARM] compliance-loop-dev-intake-failed"
    assert create.call_args.kwargs["labels"] == ["operations-alarm", "auto-escalation"]
    # The region code comes from the ARN, so the console link works.
    assert "region=us-east-1#alarmsV2:alarm/compliance-loop-dev-intake-failed" in body
    comment.assert_not_called()


def test_a_refire_comments_on_the_open_issue_instead_of_opening_another(handler):
    _, _, create, comment = run_alarm(handler, transition("ALARM"), open_issue=12)
    create.assert_not_called()
    assert comment.call_args.args[3] == 12
    assert "Disparou de novo" in comment.call_args.args[4]


def test_back_to_ok_comments_and_leaves_the_issue_open(handler):
    """OK means the metric recovered, not that someone handled the cause."""
    _, _, create, comment = run_alarm(handler, transition("OK"), open_issue=12)
    create.assert_not_called()
    assert "Voltou a OK" in comment.call_args.args[4]


def test_ok_with_no_open_issue_changes_nothing(handler):
    _, _, create, comment = run_alarm(handler, transition("OK"), open_issue=None)
    create.assert_not_called()
    comment.assert_not_called()


def test_a_failed_github_call_keeps_the_notification_queued(handler):
    with patch.object(handler, "get_github_params", return_value=PARAMS), \
         patch.object(handler, "open_alarm_issue", side_effect=OSError("github down")):
        out = handler.lambda_handler({"Records": [transition("ALARM")]}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "a1"}]}
