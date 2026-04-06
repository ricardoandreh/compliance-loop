"""
Property-Based Tests for Execute Lambda

Tests correctness properties using Hypothesis to generate random test cases.
Each property validates specific requirements with minimum 100 iterations.

Properties tested:
- Property 15: Execute Idempotency (Requirements 7.7, 9.1, 11.5)
- Property 16: Execute OpenTofu Invocation (Requirements 7.1, 7.2, 7.3)
- Property 17: Execute Success Response (Requirements 7.5)
- Property 18: Execute Failure Response (Requirements 7.6)
"""

import sys
import os
import json
from unittest.mock import Mock, patch, MagicMock
from subprocess import CompletedProcess, TimeoutExpired
from typing import Dict, Any

import pytest
from hypothesis import given, settings, strategies as st
from hypothesis import assume

# Load execute handler explicitly by file path to avoid module caching
# (analyze/ also has handler.py, and pytest may import it first)
import importlib.util
_execute_handler_path = os.path.join(os.path.dirname(__file__), '../../lambdas/execute/handler.py')
_execute_handler_path = os.path.abspath(_execute_handler_path)

# Remove any previously cached 'execute_handler' module
sys.modules.pop('execute_handler', None)

_spec = importlib.util.spec_from_file_location('execute_handler', _execute_handler_path)
handler = importlib.util.module_from_spec(_spec)
sys.modules['execute_handler'] = handler
_spec.loader.exec_module(handler)
lambda_handler = handler.lambda_handler
extract_bucket_name = handler.extract_bucket_name
del _execute_handler_path, _spec


# ============================================================================
# Hypothesis Strategies
# ============================================================================

@st.composite
def bucket_name_strategy(draw):
    """Generate valid S3 bucket names."""
    # S3 bucket naming rules:
    # - 3-63 characters
    # - lowercase letters, numbers, hyphens, dots
    # - must start and end with letter or number
    # - no consecutive dots
    length = draw(st.integers(min_value=3, max_value=63))
    
    # Generate bucket name with valid characters
    chars = draw(st.lists(
        st.sampled_from('abcdefghijklmnopqrstuvwxyz0123456789-.'),
        min_size=length,
        max_size=length
    ))
    name = ''.join(chars)
    
    # Ensure valid bucket name
    assume(len(name) >= 3)
    assume(name[0].isalnum())
    assume(name[-1].isalnum())
    assume('..' not in name)
    assume('.-' not in name)
    assume('-.' not in name)
    
    return name


@st.composite
def resource_arn_strategy(draw):
    """Generate valid S3 resource ARNs."""
    bucket_name = draw(bucket_name_strategy())
    return f"arn:aws:s3:::{bucket_name}"


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
    """Generate valid Execute Lambda input event."""
    return {
        'resource_arn': draw(resource_arn_strategy()),
        'region': draw(aws_region_strategy()),
        'violation_id': draw(st.uuids()).hex,
        'control_id': 'ctrl-s3-public-block'
    }


@st.composite
def tofu_success_output_strategy(draw):
    """Generate realistic OpenTofu success output."""
    templates = [
        "aws_s3_bucket_public_access_block.remediation: Creating...",
        "aws_s3_bucket_public_access_block.remediation: Creation complete after {time}s",
        "aws_s3_bucket_public_access_block.remediation: Refreshing state...\n\nNo changes. Your infrastructure matches the configuration.",
        "aws_s3_bucket_public_access_block.remediation: Modifying...\naws_s3_bucket_public_access_block.remediation: Modifications complete after {time}s",
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
    AND the final resource state MUST be the same
    
    Validates Requirements:
    - 7.7: Execute SHALL operate idempotently on the same resource
    - 9.1: Execute SHALL produce the same final resource state when invoked multiple times
    - 11.5: (Implicit) Multiple executions don't cause configuration drift
    """
    # Mock file system operations and subprocess
    with patch('execute_handler.shutil.rmtree'), \
         patch('execute_handler.os.path.exists', return_value=False), \
         patch('execute_handler.os.makedirs'), \
         patch('execute_handler.shutil.copy'), \
         patch('execute_handler.shutil.copytree'), \
         patch('execute_handler.subprocess.run') as mock_run:
        
        # Mock successful tofu apply (idempotent)
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=0,
            stdout=tofu_output,
            stderr=""
        )
        
        # Execute handler multiple times
        results = []
        for i in range(num_invocations):
            result = lambda_handler(event, None)
            results.append(result)
        
        # Verify all invocations return success
        for i, result in enumerate(results):
            assert result['remediation_status'] == 'success', \
                f"Invocation {i+1} failed: {result}"
            assert 'error' not in result, \
                f"Invocation {i+1} should not have error field"
        
        # Verify subprocess was called correct number of times
        assert mock_run.call_count == num_invocations, \
            f"Expected {num_invocations} subprocess calls, got {mock_run.call_count}"
        
        # Verify all calls used same parameters (idempotent configuration)
        calls = mock_run.call_args_list
        first_call_args = calls[0][0][0]
        for i, call in enumerate(calls[1:], start=2):
            assert call[0][0] == first_call_args, \
                f"Invocation {i} used different arguments than invocation 1"
        
        # Verify -refresh=false is used (prevents state drift detection)
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
      - resource_id extracted from resource_arn
      - region from event
      - -refresh=false flag
      - correct working directory and plugin directory
    
    Validates Requirements:
    - 7.1: Execute SHALL apply OpenTofu configuration to the resource
    - 7.2: Execute SHALL use resource_id from Orchestrator to identify target resource
    - 7.3: Execute SHALL apply OpenTofu configuration with -refresh=false flag
    """
    bucket_name = extract_bucket_name(event['resource_arn'])
    region = event['region']
    
    # Mock file system operations and subprocess
    with patch('execute_handler.shutil.rmtree'), \
         patch('execute_handler.os.path.exists', return_value=False), \
         patch('execute_handler.os.makedirs'), \
         patch('execute_handler.shutil.copy'), \
         patch('execute_handler.shutil.copytree'), \
         patch('execute_handler.subprocess.run') as mock_run, \
         patch('builtins.open', create=True) as mock_open:
        
        # Mock file write for tfvars
        mock_file = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_file
        
        # Mock successful tofu apply
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=0,
            stdout=tofu_output,
            stderr=""
        )
        
        # Execute handler
        result = lambda_handler(event, None)
        
        # Verify subprocess.run was called
        assert mock_run.call_count == 1, "subprocess.run should be called exactly once"
        
        # Extract call arguments
        call_args = mock_run.call_args
        command = call_args[0][0]
        kwargs = call_args[1]
        
        # Verify command structure
        assert command[0] == 'tofu', "Command must start with 'tofu'"
        assert command[1] == 'apply', "Command must be 'tofu apply'"
        
        # Verify -refresh=false flag (Requirement 7.3)
        assert '-refresh=false' in command, \
            "Command must include -refresh=false flag"
        
        # Verify -auto-approve flag
        assert '-auto-approve' in command, \
            "Command must include -auto-approve flag"
        
        # Verify var-file parameter
        assert '-var-file=/tmp/terraform.tfvars.json' in command, \
            "Command must include -var-file parameter"
        
        # Verify state file parameter
        assert '-state=/tmp/remediation.tfstate' in command, \
            "Command must include -state parameter"
        
        # Verify plugin directory parameter
        assert '-plugin-dir=/opt/providers' in command, \
            "Command must include -plugin-dir parameter"
        
        # Verify working directory
        assert kwargs['cwd'] == '/tmp/remediation', \
            "Command must run in /tmp/remediation directory"
        
        # Verify timeout
        assert kwargs['timeout'] == 40, \
            "Command must have 40 second timeout"
        
        # Verify capture_output and text flags
        assert kwargs['capture_output'] is True, \
            "Command must capture output"
        assert kwargs['text'] is True, \
            "Command must use text mode"
        
        # Verify tfvars file was written with correct values
        mock_open.assert_called_with('/tmp/terraform.tfvars.json', 'w')
        written_data = ''.join(call.args[0] for call in mock_file.write.call_args_list)
        tfvars = json.loads(written_data)
        
        # Verify bucket_name and region in tfvars (Requirement 7.2)
        assert tfvars['bucket_name'] == bucket_name, \
            f"tfvars must contain correct bucket_name: expected {bucket_name}, got {tfvars.get('bucket_name')}"
        assert tfvars['region'] == region, \
            f"tfvars must contain correct region: expected {region}, got {tfvars.get('region')}"


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
    # Mock file system operations and subprocess
    with patch('execute_handler.shutil.rmtree'), \
         patch('execute_handler.os.path.exists', return_value=False), \
         patch('execute_handler.os.makedirs'), \
         patch('execute_handler.shutil.copy'), \
         patch('execute_handler.shutil.copytree'), \
         patch('execute_handler.subprocess.run') as mock_run:
        
        # Mock successful tofu apply
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=0,
            stdout=tofu_output,
            stderr=""
        )
        
        # Execute handler
        result = lambda_handler(event, None)
        
        # Verify success response structure
        assert 'remediation_status' in result, \
            "Response must contain remediation_status field"
        assert result['remediation_status'] == 'success', \
            f"remediation_status must be 'success', got '{result['remediation_status']}'"
        
        # Verify output field contains stdout
        assert 'output' in result, \
            "Success response must contain output field"
        assert result['output'] == tofu_output, \
            f"output field must contain OpenTofu stdout"
        
        # Verify NO error field in success response
        assert 'error' not in result, \
            "Success response must NOT contain error field"
        
        # Verify response is JSON serializable (for Step Functions)
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
    # Mock file system operations and subprocess
    with patch('execute_handler.shutil.rmtree'), \
         patch('execute_handler.os.path.exists', return_value=False), \
         patch('execute_handler.os.makedirs'), \
         patch('execute_handler.shutil.copy'), \
         patch('execute_handler.shutil.copytree'), \
         patch('execute_handler.subprocess.run') as mock_run:
        
        # Mock failed tofu apply
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=returncode,
            stdout="",
            stderr=tofu_error
        )
        
        # Execute handler
        result = lambda_handler(event, None)
        
        # Verify failure response structure
        assert 'remediation_status' in result, \
            "Response must contain remediation_status field"
        assert result['remediation_status'] == 'failed', \
            f"remediation_status must be 'failed', got '{result['remediation_status']}'"
        
        # Verify error field contains stderr
        assert 'error' in result, \
            "Failure response must contain error field"
        assert result['error'] == tofu_error, \
            f"error field must contain OpenTofu stderr"
        
        # Verify error field is non-empty
        assert len(result['error']) > 0, \
            "error field must contain error details"
        
        # Verify NO output field in failure response
        assert 'output' not in result, \
            "Failure response must NOT contain output field"
        
        # Verify response is JSON serializable (for Step Functions)
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
    THEN it MUST return:
      - remediation_status: "failed"
      - error: timeout error message
    
    Validates Requirements:
    - 7.6: If OpenTofu apply fails, Execute SHALL return failure status with error details
    """
    # Mock file system operations and subprocess
    with patch('execute_handler.shutil.rmtree'), \
         patch('execute_handler.os.path.exists', return_value=False), \
         patch('execute_handler.os.makedirs'), \
         patch('execute_handler.shutil.copy'), \
         patch('execute_handler.shutil.copytree'), \
         patch('execute_handler.subprocess.run') as mock_run:
        
        # Mock timeout exception
        mock_run.side_effect = TimeoutExpired(
            cmd=['tofu', 'apply'],
            timeout=40
        )
        
        # Execute handler
        result = lambda_handler(event, None)
        
        # Verify failure response structure
        assert result['remediation_status'] == 'failed', \
            "Timeout must result in failed status"
        
        # Verify error field contains timeout information
        assert 'error' in result, \
            "Timeout response must contain error field"
        assert 'timed out' in result['error'].lower(), \
            "Error message must indicate timeout"
        assert '40 seconds' in result['error'], \
            "Error message must include timeout duration"


# ============================================================================
# Property 18c: Execute Unexpected Exception Failure Response
# Requirements: 7.6
# ============================================================================

@settings(max_examples=100)
@given(
    event=execute_event_strategy(),
    error_message=st.text(min_size=10, max_size=200, alphabet=st.characters(whitelist_categories=('L', 'N', 'P', 'Zs')))
)
def test_property_18c_execute_unexpected_exception_failure_response(event, error_message):
    """
    Property 18c: Execute Unexpected Exception Failure Response
    
    GIVEN an unexpected exception occurs during execution
    WHEN lambda_handler completes
    THEN it MUST return:
      - remediation_status: "failed"
      - error: exception message
    
    Validates Requirements:
    - 7.6: If OpenTofu apply fails, Execute SHALL return failure status with error details
    """
    # Mock file system operations and subprocess
    with patch('execute_handler.shutil.rmtree'), \
         patch('execute_handler.os.path.exists', return_value=False), \
         patch('execute_handler.os.makedirs'), \
         patch('execute_handler.shutil.copy'), \
         patch('execute_handler.shutil.copytree'), \
         patch('execute_handler.subprocess.run') as mock_run:
        
        # Mock unexpected exception
        mock_run.side_effect = Exception(error_message)
        
        # Execute handler
        result = lambda_handler(event, None)
        
        # Verify failure response structure
        assert result['remediation_status'] == 'failed', \
            "Unexpected exception must result in failed status"
        
        # Verify error field contains exception information
        assert 'error' in result, \
            "Exception response must contain error field"
        assert 'Unexpected error' in result['error'], \
            "Error message must indicate unexpected error"
        assert error_message in result['error'], \
            "Error message must include original exception message"


# ============================================================================
# Integration Property: Warm Container Cleanup
# ============================================================================

@settings(max_examples=100)
@given(
    event=execute_event_strategy(),
    tofu_output=tofu_success_output_strategy(),
    has_previous_state=st.booleans()
)
def test_property_warm_container_cleanup(event, tofu_output, has_previous_state):
    """
    Integration property test for warm container cleanup.
    
    GIVEN a Lambda container that may have previous execution state
    WHEN lambda_handler is invoked
    THEN it MUST clean up /tmp/remediation/ before execution
    AND create fresh working directory
    
    This ensures idempotency across warm container invocations.
    """
    # Mock file system operations
    with patch('execute_handler.shutil.rmtree') as mock_rmtree, \
         patch('execute_handler.os.path.exists') as mock_exists, \
         patch('execute_handler.os.makedirs') as mock_makedirs, \
         patch('execute_handler.shutil.copy'), \
         patch('execute_handler.shutil.copytree'), \
         patch('execute_handler.subprocess.run') as mock_run:
        
        # Simulate warm container with previous state
        mock_exists.return_value = has_previous_state
        
        # Mock successful tofu apply
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=0,
            stdout=tofu_output,
            stderr=""
        )
        
        # Execute handler
        result = lambda_handler(event, None)
        
        # Verify cleanup was called if previous state existed
        if has_previous_state:
            mock_rmtree.assert_called_once_with('/tmp/remediation')
        else:
            mock_rmtree.assert_not_called()
        
        # Verify fresh directory was created
        mock_makedirs.assert_called_once_with('/tmp/remediation', exist_ok=True)
        
        # Verify execution succeeded
        assert result['remediation_status'] == 'success'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
