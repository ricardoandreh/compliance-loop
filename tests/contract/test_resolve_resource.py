"""
Run the state machine's own ResolveResource state in Step Functions.

Properties 1 and 27: every trigger the loop accepts must resolve to the right
resource_id and resource_arn, because both are keys — of the exceptions lookup,
of the audit rows, of the session policy. The expression is JSONata inside the
definition, so the only faithful way to test it is to let Step Functions
evaluate it: TestState runs a single state with a given input, without starting
an execution or touching any resource.

Needs AWS credentials (the call is read-only); skipped without them.
"""

import json
import os
import re

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

DEFINITION = os.path.join(os.path.dirname(__file__), "..", "..", "infra", "state_machine_definition.json")
FAKE = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


@pytest.fixture(scope="module")
def resolve_state():
    text = open(DEFINITION).read()
    # Template variables only appear in other states; neutral values keep the
    # document parseable.
    text = re.sub(r"\$\{[a-z_]+\}", "0", text)
    return {**json.loads(text)["States"]["ResolveResource"], "QueryLanguage": "JSONata"}


@pytest.fixture(scope="module")
def sfn():
    # Same credential handling as test_event_pattern: unit modules set
    # placeholder credentials for the whole session.
    placeholders = {k: os.environ[k] for k in FAKE if os.environ.get(k) == "testing"}
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
        pytest.skip("no AWS credentials available for Step Functions TestState")
    client = boto3.client("stepfunctions", region_name="us-east-1",
                          aws_access_key_id=frozen.access_key,
                          aws_secret_access_key=frozen.secret_key,
                          aws_session_token=frozen.token)
    try:
        client.list_state_machines(maxResults=1)
    except (BotoCoreError, ClientError) as e:
        pytest.skip(f"Step Functions not reachable with current credentials: {e}")
    return client


BASE = {"region": "us-east-1", "account_id": "123456789012", "event_time": "2026-09-24T00:00:00Z"}

CASES = {
    # S3 events name the bucket in the request.
    "bucket event": ({**BASE, "event_name": "PutBucketPublicAccessBlock",
                      "request_parameters": {"bucketName": "b1"}, "response_elements": None},
                     "b1", "arn:aws:s3:::b1", "detect"),
    # CreateVolume names the volume only in the response.
    "volume event": ({**BASE, "event_name": "CreateVolume", "request_parameters": {"size": "1"},
                      "response_elements": {"volumeId": "vol-abc"}},
                     "vol-abc", "arn:aws:ec2:us-east-1:123456789012:volume/vol-abc", "detect"),
    # Account-wide settings have no resource: the synthetic identifier (L3).
    "account event": ({**BASE, "event_name": "DisableEbsEncryptionByDefault",
                       "request_parameters": {}, "response_elements": None},
                      "account-level", "arn:aws:us-east-1:123456789012", "detect"),
    # The Scheduler and the verification round already carry the resource.
    "exception expiry": ({**BASE, "trigger": "exception_ttl_expiry", "control_id": "ctrl-x",
                          "resource_id": "b2", "resource_arn": "arn:aws:s3:::b2"},
                         "b2", "arn:aws:s3:::b2", "detect"),
    "verification round": ({**BASE, "mode": "verify", "resource_id": "b3",
                            "resource_arn": "arn:aws:s3:::b3", "origin_execution": "arn:o",
                            "controls": [{"control_id": {"S": "ctrl-x"}}]},
                           "b3", "arn:aws:s3:::b3", "verify"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_trigger_resolves_to_the_right_resource(sfn, resolve_state, case):
    event, resource_id, resource_arn, mode = CASES[case]
    out = sfn.test_state(definition=json.dumps(resolve_state), input=json.dumps(event))
    assert out["status"] == "SUCCEEDED", out.get("cause")
    resolved = json.loads(out["output"])
    assert resolved["resource_id"] == resource_id
    assert resolved["resource_arn"] == resource_arn
    assert resolved["mode"] == mode
