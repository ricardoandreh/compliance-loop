"""
The EventBridge pattern controls-sync writes decides what wakes the loop.

Two failure modes matter, and both are silent: a pattern that lets the
remediation's own events through brings the echo back, and a pattern that
filters too broadly stops detecting violations altogether. See also
tests/contract/test_event_pattern.py, which evaluates the generated pattern in
EventBridge itself.
"""

import importlib.util
import os

ROLE = "arn:aws:iam::123456789012:role/compliance-loop-execute-lambda-dev"

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("CONTROLS_TABLE", "controls")
os.environ.setdefault("EVENTBRIDGE_RULE_NAME", "rule")
os.environ.setdefault("REMEDIATION_ROLE_ARN", ROLE)

_spec = importlib.util.spec_from_file_location(
    "controls_sync_handler_unit",
    os.path.join(os.path.dirname(__file__), "../../lambdas/controls_sync/handler.py"),
)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def test_event_names_are_unique_and_sorted():
    p = handler.build_pattern(["PutBucketPolicy", "CreateBucket", "CreateBucket"], ROLE)
    assert p["detail"]["eventName"] == ["CreateBucket", "PutBucketPolicy"]


def test_remediation_role_is_excluded():
    branches = handler.build_pattern(["CreateBucket"], ROLE)["detail"]["$or"]
    arns = [b["userIdentity"]["sessionContext"]["sessionIssuer"]["arn"][0] for b in branches]
    assert {"anything-but": ROLE} in arns


def test_identities_without_a_session_issuer_still_match():
    """Regression guard for the trap measured in EventBridge: IAM users have no
    sessionIssuer, and a bare anything-but drops them — every console
    violation would go undetected."""
    branches = handler.build_pattern(["CreateBucket"], ROLE)["detail"]["$or"]
    arns = [b["userIdentity"]["sessionContext"]["sessionIssuer"]["arn"][0] for b in branches]
    assert {"exists": False} in arns
    assert len(branches) == 2


def test_only_events_emitted_by_aws_match():
    """detail-type is free text anyone with events:PutEvents can publish; the
    source prefix is what PutEvents cannot forge."""
    assert handler.build_pattern(["CreateBucket"], ROLE)["source"] == [{"prefix": "aws."}]


def _pages(*pages):
    from unittest.mock import MagicMock
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Items": items} for items in pages]
    client = MagicMock()
    client.get_paginator.return_value = paginator
    return client


def _control(*names):
    return {"cloudtrail_events": {"SS": list(names)}}


def test_controls_on_every_page_are_watched():
    """A scan returns at most 1 MB per page; a control on the second page must
    still reach the rule (design.md, L2)."""
    from unittest.mock import MagicMock, patch
    events = MagicMock()
    with patch.object(handler, "dynamodb", _pages([_control("CreateBucket")],
                                                  [_control("CreateVolume")])), \
         patch.object(handler, "events", events):
        handler.lambda_handler({}, None)
    pattern = __import__("json").loads(events.put_rule.call_args.kwargs["EventPattern"])
    assert pattern["detail"]["eventName"] == ["CreateBucket", "CreateVolume"]


def test_an_empty_table_disables_the_rule_instead_of_failing():
    """EventBridge rejects an empty eventName list. Building one would fail on
    every record of the stream; with nothing to watch, the rule is disabled."""
    from unittest.mock import MagicMock, patch
    events = MagicMock()
    with patch.object(handler, "dynamodb", _pages([])), patch.object(handler, "events", events):
        handler.lambda_handler({}, None)
    events.disable_rule.assert_called_once_with(Name="rule")
    events.put_rule.assert_not_called()
