"""
Property-Based Tests for the Execute Lambda (zip + tofu layer + S3 templates).

Tests correctness properties using Hypothesis to generate random test cases.
Each property validates specific requirements with minimum 100 iterations.

Properties tested:
- Property 15: Execute Idempotency (Requirements 7.7, 9.1, 11.5)
- Property 16: Execute OpenTofu Invocation (Requirements 7.1, 7.2, 7.3)
- Property 17: Execute Success Response (Requirements 7.5)
- Property 18: Execute Failure Response (Requirements 7.6)
- Warm-container property: no tfstate survives into a new invocation
"""

import sys
import os
import json
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from unittest.mock import patch, MagicMock
from subprocess import CompletedProcess, TimeoutExpired

import pytest
from hypothesis import given, settings, strategies as st
from hypothesis import assume

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'testing')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'testing')
os.environ.setdefault('REMEDIATION_BUCKET', 'test-remediation-bucket')

# Load execute handler explicitly by file path to avoid module caching
# (analyze/ also has handler.py, and pytest may import it first)
import importlib.util
_execute_handler_path = os.path.join(os.path.dirname(__file__), '../../lambdas/execute/handler.py')
_execute_handler_path = os.path.abspath(_execute_handler_path)

sys.modules.pop('execute_handler', None)

_spec = importlib.util.spec_from_file_location('execute_handler', _execute_handler_path)
handler = importlib.util.module_from_spec(_spec)
sys.modules['execute_handler'] = handler
_spec.loader.exec_module(handler)
lambda_handler = handler.lambda_handler
setup_remediation = handler.setup_remediation
del _execute_handler_path, _spec


# ============================================================================
# Hypothesis Strategies
# ============================================================================

@st.composite
def bucket_name_strategy(draw):
    """Generate valid S3 bucket names."""
    length = draw(st.integers(min_value=3, max_value=63))
    chars = draw(st.lists(
        st.sampled_from('abcdefghijklmnopqrstuvwxyz0123456789-.'),
        min_size=length,
        max_size=length
    ))
    name = ''.join(chars)

    assume(len(name) >= 3)
    assume(name[0].isalnum())
    assume(name[-1].isalnum())
    assume('..' not in name)
    assume('.-' not in name)
    assume('-.' not in name)

    return name


@st.composite
def aws_region_strategy(draw):
    """Generate valid AWS region names."""
    return draw(st.sampled_from([
        'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
        'eu-west-1', 'eu-west-2', 'eu-central-1',
        'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1',
        'sa-east-1', 'ca-central-1'
    ]))


@st.composite
def execute_event_strategy(draw):
    """Generate a valid Execute Lambda input event (Step Functions payload)."""
    bucket_name = draw(bucket_name_strategy())
    control_id = 'ctrl-s3-public-block'
    return {
        'resource_arn': f'arn:aws:s3:::{bucket_name}',
        'resource_id': bucket_name,
        'region': draw(aws_region_strategy()),
        'violation_id': draw(st.uuids()).hex,
        'control_id': control_id,
        'remediation_s3_key': f'controls/{control_id}/remediation.tf',
    }


@st.composite
def tofu_success_output_strategy(draw):
    """Generate realistic OpenTofu success output."""
    templates = [
        "aws_s3_bucket_public_access_block.remediation: Creating...",
        "aws_s3_bucket_public_access_block.remediation: Creation complete after {time}s",
        "No changes. Your infrastructure matches the configuration.",
        "Apply complete! Resources: 1 added, 0 changed, 0 destroyed.",
        "Apply complete! Resources: 0 added, 1 changed, 0 destroyed.",
        "Apply complete! Resources: 0 added, 0 changed, 0 destroyed."
    ]

    template = draw(st.sampled_from(templates))
    if '{time}' in template:
        time_value = draw(st.floats(min_value=0.1, max_value=10.0))
        template = template.format(time=f"{time_value:.1f}")

    return template


@st.composite
def tofu_failure_output_strategy(draw):
    """Generate realistic OpenTofu failure output."""
    error_templates = [
        "Error: error creating S3 Bucket Public Access Block: NoSuchBucket: The specified bucket does not exist",
        "Error: error creating S3 Bucket Public Access Block: AccessDenied: Access Denied\n\tstatus code: 403",
        "Error: error creating S3 Bucket Public Access Block: InvalidBucketName: The specified bucket is not valid",
        "Error: error putting S3 Bucket Public Access Block: AccessDenied: User is not authorized to perform: s3:PutBucketPublicAccessBlock",
        "Error: timeout while waiting for state to become 'success'",
        "Error: Provider configuration error: invalid credentials"
    ]

    return draw(st.sampled_from(error_templates))


# ============================================================================
# Test harness
# ============================================================================

@contextmanager
def execute_harness():
    """Bypass S3/provider setup and hand the handler a real, fresh work_dir.

    Yields (work_dir, mock_run). The tfvars file is written to the real
    work_dir so assertions can parse actual JSON instead of mocking open().
    """
    work_dir = tempfile.mkdtemp(prefix='execute-prop-')
    try:
        with patch.object(handler, 'ensure_provider_cache'), \
             patch.object(handler, 'setup_remediation', return_value=work_dir), \
             patch.object(handler.subprocess, 'run') as mock_run:
            yield work_dir, mock_run
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def read_tfvars(work_dir):
    with open(f'{work_dir}/terraform.tfvars.json') as f:
        return json.load(f)


# ============================================================================
# Property 15: Execute Idempotency
# Requirements: 7.7, 9.1, 11.5
# ============================================================================

@settings(max_examples=100)
@given(
    event=execute_event_strategy(),
    tofu_output=tofu_success_output_strategy(),
    num_invocations=st.integers(min_value=2, max_value=5)
)
def test_property_15_execute_idempotency(event, tofu_output, num_invocations):
    """
    Property 15: Execute Idempotency

    GIVEN the same resource and successful OpenTofu execution
    WHEN lambda_handler is invoked multiple times
    THEN all invocations MUST return success status
    AND all invocations MUST use identical OpenTofu arguments

    Validates Requirements:
    - 7.7: Execute SHALL operate idempotently on the same resource
    - 9.1: Execute SHALL produce the same final resource state when invoked multiple times
    - 11.5: (Implicit) Multiple executions don't cause configuration drift
    """
    with execute_harness() as (work_dir, mock_run):
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'], returncode=0, stdout=tofu_output, stderr="")

        results = [lambda_handler(event, None) for _ in range(num_invocations)]

        for i, result in enumerate(results):
            assert result['remediation_status'] == 'success', \
                f"Invocation {i+1} failed: {result}"
            assert 'error' not in result, \
                f"Invocation {i+1} should not have error field"

        assert mock_run.call_count == num_invocations

        calls = mock_run.call_args_list
        first_call_args = calls[0][0][0]
        for i, call in enumerate(calls[1:], start=2):
            assert call[0][0] == first_call_args, \
                f"Invocation {i} used different arguments than invocation 1"

        assert '-refresh=false' in first_call_args, \
            "Idempotent execution must use -refresh=false"


# ============================================================================
# Property 16: Execute OpenTofu Invocation
# Requirements: 7.1, 7.2, 7.3
# ============================================================================

@settings(max_examples=100)
@given(
    event=execute_event_strategy(),
    tofu_output=tofu_success_output_strategy()
)
def test_property_16_execute_opentofu_invocation(event, tofu_output):
    """
    Property 16: Execute OpenTofu Invocation

    GIVEN any valid Execute event
    WHEN lambda_handler processes the event
    THEN it MUST invoke OpenTofu with correct parameters:
      - bucket name derived from resource_arn
      - region from event
      - -refresh=false flag
      - per-control/per-resource state path, correct working directory

    Validates Requirements:
    - 7.1: Execute SHALL apply OpenTofu configuration to the resource
    - 7.2: Execute SHALL use resource_id from Orchestrator to identify target resource
    - 7.3: Execute SHALL apply OpenTofu configuration with -refresh=false flag
    """
    bucket_name = event['resource_arn'].split(':::')[1]
    control_id = event['control_id']

    with execute_harness() as (work_dir, mock_run):
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'], returncode=0, stdout=tofu_output, stderr="")

        lambda_handler(event, None)

        assert mock_run.call_count == 1, "subprocess.run should be called exactly once"

        call_args = mock_run.call_args
        command = call_args[0][0]
        kwargs = call_args[1]

        assert command[0] == '/opt/bin/tofu', "Command must invoke the layer's tofu binary"
        assert command[1] == 'apply', "Command must be 'tofu apply'"
        assert '-refresh=false' in command, "Command must include -refresh=false flag"
        assert '-auto-approve' in command, "Command must include -auto-approve flag"
        assert '-no-color' in command, "Command must include -no-color flag"
        assert f'-var-file={work_dir}/terraform.tfvars.json' in command, \
            "Command must include -var-file pointing into the work_dir"
        assert f'-state=/tmp/{control_id}/{bucket_name}.tfstate' in command, \
            "State path must be isolated per control and resource"

        assert kwargs['cwd'] == work_dir, "Command must run in the prepared work_dir"
        assert kwargs['timeout'] == 65, \
            "Subprocess timeout must be 65s (leaves headroom inside the 90s Lambda)"
        assert kwargs['capture_output'] is True
        assert kwargs['text'] is True

        tfvars = read_tfvars(work_dir)
        assert tfvars['bucket_name'] == bucket_name, \
            f"tfvars must contain correct bucket_name: expected {bucket_name}"
        assert tfvars['region'] == event['region'], \
            f"tfvars must contain correct region: expected {event['region']}"


# ============================================================================
# Property 17: Execute Success Response
# Requirements: 7.5
# ============================================================================

@settings(max_examples=100)
@given(
    event=execute_event_strategy(),
    tofu_output=tofu_success_output_strategy()
)
def test_property_17_execute_success_response(event, tofu_output):
    """
    Property 17: Execute Success Response

    GIVEN OpenTofu apply succeeds (returncode 0)
    WHEN lambda_handler completes
    THEN it MUST return:
      - remediation_status: "success"
      - output: stdout from OpenTofu
      - NO error field

    Validates Requirements:
    - 7.5: When OpenTofu apply succeeds, Execute SHALL return success status to Orchestrator
    """
    with execute_harness() as (work_dir, mock_run):
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'], returncode=0, stdout=tofu_output, stderr="")

        result = lambda_handler(event, None)

        assert result['remediation_status'] == 'success'
        assert result['output'] == tofu_output, "output field must contain OpenTofu stdout"
        assert 'error' not in result, "Success response must NOT contain error field"

        try:
            json.dumps(result)
        except (TypeError, ValueError) as e:
            pytest.fail(f"Response must be JSON serializable: {e}")


# ============================================================================
# Property 18: Execute Failure Response
# Requirements: 7.6
# ============================================================================

@settings(max_examples=100)
@given(
    event=execute_event_strategy(),
    tofu_error=tofu_failure_output_strategy(),
    returncode=st.integers(min_value=1, max_value=255)
)
def test_property_18_execute_failure_response(event, tofu_error, returncode):
    """
    Property 18: Execute Failure Response

    GIVEN OpenTofu apply fails (returncode != 0)
    WHEN lambda_handler completes
    THEN it MUST return:
      - remediation_status: "failed"
      - error: stderr from OpenTofu
      - NO output field

    Validates Requirements:
    - 7.6: If OpenTofu apply fails, Execute SHALL return failure status with error details
    """
    with execute_harness() as (work_dir, mock_run):
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'], returncode=returncode, stdout="", stderr=tofu_error)

        result = lambda_handler(event, None)

        assert result['remediation_status'] == 'failed'
        assert result['error'] == tofu_error, "error field must contain OpenTofu stderr"
        assert len(result['error']) > 0
        assert 'output' not in result, "Failure response must NOT contain output field"

        try:
            json.dumps(result)
        except (TypeError, ValueError) as e:
            pytest.fail(f"Response must be JSON serializable: {e}")


# ============================================================================
# Property 18b: Execute Timeout Failure Response
# Requirements: 7.6
# ============================================================================

@settings(max_examples=100)
@given(event=execute_event_strategy())
def test_property_18b_execute_timeout_failure_response(event):
    """
    Property 18b: Execute Timeout Failure Response

    GIVEN OpenTofu apply times out
    WHEN lambda_handler completes
    THEN it MUST return remediation_status "failed" with a timeout error message

    Validates Requirements:
    - 7.6: If OpenTofu apply fails, Execute SHALL return failure status with error details
    """
    with execute_harness() as (work_dir, mock_run):
        mock_run.side_effect = TimeoutExpired(cmd=['tofu', 'apply'], timeout=65)

        result = lambda_handler(event, None)

        assert result['remediation_status'] == 'failed', \
            "Timeout must result in failed status"
        assert 'error' in result
        assert 'timed out' in result['error'].lower(), \
            "Error message must indicate timeout"


# ============================================================================
# Property 18c: Execute Setup Failure Response
# Requirements: 7.6
# ============================================================================

@settings(max_examples=100)
@given(
    event=execute_event_strategy(),
    error_message=st.text(min_size=10, max_size=200,
                          alphabet=st.characters(whitelist_categories=('L', 'N', 'P', 'Zs')))
)
def test_property_18c_execute_setup_failure_response(event, error_message):
    """
    Property 18c: Execute Setup Failure Response

    GIVEN template download or provider cache setup fails
    WHEN lambda_handler completes
    THEN it MUST return remediation_status "failed" with the setup error,
    AND OpenTofu MUST NOT be invoked

    Validates Requirements:
    - 7.6: If remediation cannot proceed, Execute SHALL return failure status
      with error details
    """
    with patch.object(handler, 'ensure_provider_cache'), \
         patch.object(handler, 'setup_remediation', side_effect=RuntimeError(error_message)), \
         patch.object(handler.subprocess, 'run') as mock_run:

        result = lambda_handler(event, None)

        assert result['remediation_status'] == 'failed'
        assert 'Setup failed' in result['error']
        assert error_message in result['error']
        mock_run.assert_not_called()


# ============================================================================
# Warm-container property: no tfstate survives into a new invocation
# ============================================================================

@settings(max_examples=100)
@given(
    bucket_name=bucket_name_strategy(),
    has_previous_state=st.booleans()
)
def test_property_warm_container_clean_state(bucket_name, has_previous_state):
    """
    Warm-container cleanup property.

    GIVEN a Lambda container that may hold a tfstate from a previous invocation
    WHEN setup_remediation prepares the work_dir
    THEN no pre-existing tfstate survives — combined with -refresh=false, a
    stale state would make OpenTofu report 'No changes' for a re-violated
    resource, skipping remediation while reporting success.
    """
    control_id = f'prop-{uuid.uuid4().hex[:8]}'
    work_dir = f'/tmp/{control_id}'
    tmp = tempfile.mkdtemp(prefix='execute-cache-')
    try:
        tf_cache = os.path.join(tmp, 'tf-dot-terraform')
        os.makedirs(tf_cache)
        tf_lock = os.path.join(tmp, 'tf-lock.hcl')
        with open(tf_lock, 'w') as f:
            f.write('# lock')

        if has_previous_state:
            os.makedirs(work_dir, exist_ok=True)
            with open(f'{work_dir}/{bucket_name}.tfstate', 'w') as f:
                f.write('{"resources": ["stale"]}')

        with patch.object(handler, 's3', MagicMock()), \
             patch.object(handler, 'TF_CACHE', tf_cache), \
             patch.object(handler, 'TF_LOCK', tf_lock):
            returned = setup_remediation(control_id, f'controls/{control_id}/remediation.tf')

        assert returned == work_dir
        leftover = [f for f in os.listdir(work_dir) if f.endswith('.tfstate')]
        assert leftover == [], \
            f"stale tfstate must not survive setup_remediation: {leftover}"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
