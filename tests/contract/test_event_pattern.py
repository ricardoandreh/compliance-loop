"""
Evaluate the pattern controls-sync generates in EventBridge itself.

The unit test checks the pattern's shape; this one checks its meaning, with
`TestEventPattern` — the same matcher the live rule uses. The distinction is
not academic: an anything-but on a field that is absent does not match, which
is invisible in the JSON and only shows up when EventBridge evaluates it.

Needs AWS credentials (the API call is read-only and touches no resource);
skipped without them.
"""

import importlib.util
import json
import os

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

ROLE = "arn:aws:iam::123456789012:role/compliance-loop-execute-lambda-dev"

os.environ.setdefault("CONTROLS_TABLE", "controls")
os.environ.setdefault("EVENTBRIDGE_RULE_NAME", "rule")
os.environ.setdefault("REMEDIATION_ROLE_ARN", ROLE)
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

_spec = importlib.util.spec_from_file_location(
    "controls_sync_handler_contract",
    os.path.join(os.path.dirname(__file__), "../../lambdas/controls_sync/handler.py"),
)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def event(user_identity):
    return json.dumps({
        "id": "1", "account": "123456789012", "source": "aws.s3",
        "time": "2026-09-12T00:00:00Z", "region": "us-east-1", "resources": [],
        "detail-type": "AWS API Call via CloudTrail",
        "detail": {"eventName": "PutBucketPublicAccessBlock", "userIdentity": user_identity},
    })


IDENTITIES = {
    # The shapes observed in this account's CloudTrail.
    "iam_user": ({"type": "IAMUser", "arn": "arn:aws:iam::123456789012:user/someone"}, True),
    "root": ({"type": "Root", "arn": "arn:aws:iam::123456789012:root"}, True),
    "other_role": ({"type": "AssumedRole", "sessionContext": {"sessionIssuer": {
        "arn": "arn:aws:iam::123456789012:role/terraform-ci"}}}, True),
    "remediation": ({"type": "AssumedRole", "sessionContext": {"sessionIssuer": {
        "arn": ROLE}}}, False),
}


FAKE_CREDENTIAL_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


@pytest.fixture(scope="module")
def client():
    # Unit-test modules set placeholder credentials ("testing") at import time,
    # which happens for the whole suite before any test runs; boto3 prefers
    # environment credentials, so this test would silently skip in a full run
    # while passing on its own. Resolve the real chain with the placeholders
    # set aside, freeze those credentials into the client, and put the
    # environment back untouched.
    placeholders = {k: os.environ[k] for k in FAKE_CREDENTIAL_VARS
                    if os.environ.get(k) == "testing"}
    for k in placeholders:
        del os.environ[k]
    try:
        creds = boto3.Session().get_credentials()
        frozen = creds.get_frozen_credentials() if creds else None
    except BotoCoreError:
        frozen = None
    finally:
        os.environ.update(placeholders)

    if frozen is None:
        pytest.skip("no AWS credentials available for EventBridge TestEventPattern")
    c = boto3.client("events", region_name="us-east-1",
                     aws_access_key_id=frozen.access_key,
                     aws_secret_access_key=frozen.secret_key,
                     aws_session_token=frozen.token)
    try:
        c.test_event_pattern(EventPattern='{"source":["aws.s3"]}', Event=event({}))
    except (BotoCoreError, ClientError) as e:
        pytest.skip(f"EventBridge not reachable with current credentials: {e}")
    return c


@pytest.mark.parametrize("who", sorted(IDENTITIES))
def test_generated_pattern_matches_everyone_but_the_remediation(client, who):
    identity, should_match = IDENTITIES[who]
    pattern = json.dumps(handler.build_pattern(["PutBucketPublicAccessBlock"], ROLE))
    result = client.test_event_pattern(EventPattern=pattern, Event=event(identity))["Result"]
    assert result is should_match, (
        f"{who}: expected match={should_match}, EventBridge says {result}")
