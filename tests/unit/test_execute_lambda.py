"""
Unit tests for Execute Lambda handler.

Tests remediation logic with mocked subprocess calls to OpenTofu.
Validates success cases, failure cases, idempotency, and error handling.

Requirements tested: 7.5, 7.6, 7.7, 9.1
"""

import pytest
import json
import os
import shutil
from unittest.mock import Mock, patch, MagicMock, call
from subprocess import CompletedProcess, TimeoutExpired

# Import the handler module
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../lambdas/execute'))
from handler import lambda_handler, extract_bucket_name


class TestExtractBucketName:
    """Test bucket name extraction from ARN."""
    
    def test_extract_bucket_name_standard(self):
        """Test extraction from standard S3 ARN."""
        arn = "arn:aws:s3:::my-test-bucket"
        assert extract_bucket_name(arn) == "my-test-bucket"
    
    def test_extract_bucket_name_with_dots(self):
        """Test extraction from ARN with dots in bucket name."""
        arn = "arn:aws:s3:::my.test.bucket"
        assert extract_bucket_name(arn) == "my.test.bucket"
    
    def test_extract_bucket_name_with_dashes(self):
        """Test extraction from ARN with dashes in bucket name."""
        arn = "arn:aws:s3:::my-test-bucket-123"
        assert extract_bucket_name(arn) == "my-test-bucket-123"


class TestExecuteLambdaHandler:
    """Test Execute Lambda handler with mocked subprocess calls."""
    
    @pytest.fixture
    def mock_event(self):
        """Standard event fixture."""
        return {
            "resource_arn": "arn:aws:s3:::non-compliant-bucket",
            "region": "us-east-1"
        }
    
    @pytest.fixture
    def mock_context(self):
        """Mock Lambda context."""
        context = Mock()
        context.function_name = "execute-lambda"
        context.request_id = "test-request-id"
        return context
    
    @pytest.fixture(autouse=True)
    def setup_teardown(self):
        """Setup and teardown for each test."""
        # Setup: ensure /tmp/remediation doesn't exist
        if os.path.exists('/tmp/remediation'):
            shutil.rmtree('/tmp/remediation')
        
        yield
        
        # Teardown: clean up /tmp/remediation
        if os.path.exists('/tmp/remediation'):
            shutil.rmtree('/tmp/remediation')
        if os.path.exists('/tmp/terraform.tfvars.json'):
            os.remove('/tmp/terraform.tfvars.json')
    
    @patch('handler.shutil.copytree')
    @patch('handler.shutil.copy')
    @patch('handler.subprocess.run')
    def test_non_compliant_bucket_remediated_successfully(
        self, mock_run, mock_copy, mock_copytree, mock_event, mock_context
    ):
        """
        Test: bucket não-compliant é remediado com sucesso.
        
        Requirement 7.5: Execute SHALL return success status when OpenTofu apply succeeds.
        """
        # Mock successful tofu apply
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=0,
            stdout="aws_s3_bucket_public_access_block.remediation: Creation complete after 2s",
            stderr=""
        )
        
        # Execute handler
        result = lambda_handler(mock_event, mock_context)
        
        # Verify result
        assert result['remediation_status'] == 'success'
        assert 'Creation complete' in result['output']
        assert 'error' not in result
        
        # Verify subprocess.run was called with correct arguments
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == [
            'tofu', 'apply',
            '-refresh=false',
            '-auto-approve',
            '-var-file=/tmp/terraform.tfvars.json',
            '-state=/tmp/remediation.tfstate',
            '-plugin-dir=/opt/providers'
        ]
        assert call_args[1]['cwd'] == '/tmp/remediation'
        assert call_args[1]['timeout'] == 40
        
        # Verify tfvars file was created
        assert os.path.exists('/tmp/terraform.tfvars.json')
        with open('/tmp/terraform.tfvars.json', 'r') as f:
            tfvars = json.load(f)
            assert tfvars['bucket_name'] == 'non-compliant-bucket'
            assert tfvars['region'] == 'us-east-1'
    
    @patch('handler.shutil.copytree')
    @patch('handler.shutil.copy')
    @patch('handler.subprocess.run')
    def test_compliant_bucket_returns_success_idempotency(
        self, mock_run, mock_copy, mock_copytree, mock_event, mock_context
    ):
        """
        Test: bucket já compliant retorna success (idempotência).
        
        Requirement 7.7: Execute SHALL operate idempotently on the same resource.
        Requirement 9.1: Execute SHALL produce the same final resource state when invoked multiple times.
        """
        # Mock tofu apply on already compliant bucket (no changes)
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=0,
            stdout="aws_s3_bucket_public_access_block.remediation: Refreshing state...\n\nNo changes. Your infrastructure matches the configuration.",
            stderr=""
        )
        
        # Execute handler
        result = lambda_handler(mock_event, mock_context)
        
        # Verify result indicates success (idempotent operation)
        assert result['remediation_status'] == 'success'
        assert 'No changes' in result['output']
        
        # Execute again to verify idempotency
        result2 = lambda_handler(mock_event, mock_context)
        assert result2['remediation_status'] == 'success'
        assert result2['output'] == result['output']
    
    @patch('handler.shutil.copytree')
    @patch('handler.shutil.copy')
    @patch('handler.subprocess.run')
    def test_nonexistent_bucket_returns_failure(
        self, mock_run, mock_copy, mock_copytree, mock_context
    ):
        """
        Test: bucket inexistente retorna failure.
        
        Requirement 7.6: Execute SHALL return failure status with error details when OpenTofu apply fails.
        """
        event = {
            "resource_arn": "arn:aws:s3:::nonexistent-bucket",
            "region": "us-east-1"
        }
        
        # Mock tofu apply failure for nonexistent bucket
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=1,
            stdout="",
            stderr="Error: error creating S3 Bucket Public Access Block: NoSuchBucket: The specified bucket does not exist"
        )
        
        # Execute handler
        result = lambda_handler(event, mock_context)
        
        # Verify result indicates failure
        assert result['remediation_status'] == 'failed'
        assert 'error' in result
        assert 'NoSuchBucket' in result['error']
        assert 'does not exist' in result['error']
    
    @patch('handler.shutil.copytree')
    @patch('handler.shutil.copy')
    @patch('handler.subprocess.run')
    def test_iam_permission_error_returns_failure(
        self, mock_run, mock_copy, mock_copytree, mock_event, mock_context
    ):
        """
        Test: erro de permissão IAM retorna failure com erro.
        
        Requirement 7.6: Execute SHALL return failure status with error details when OpenTofu apply fails.
        """
        # Mock tofu apply failure due to IAM permissions
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=1,
            stdout="",
            stderr="Error: error creating S3 Bucket Public Access Block: AccessDenied: Access Denied\n\tstatus code: 403"
        )
        
        # Execute handler
        result = lambda_handler(mock_event, mock_context)
        
        # Verify result indicates failure with permission error
        assert result['remediation_status'] == 'failed'
        assert 'error' in result
        assert 'AccessDenied' in result['error']
        assert '403' in result['error']
    
    @patch('handler.shutil.copytree')
    @patch('handler.shutil.copy')
    @patch('handler.subprocess.run')
    def test_opentofu_timeout_returns_failure(
        self, mock_run, mock_copy, mock_copytree, mock_event, mock_context
    ):
        """
        Test: timeout de OpenTofu retorna failure.
        
        Requirement 7.6: Execute SHALL return failure status with error details when OpenTofu apply fails.
        """
        # Mock tofu apply timeout
        mock_run.side_effect = TimeoutExpired(
            cmd=['tofu', 'apply'],
            timeout=40
        )
        
        # Execute handler
        result = lambda_handler(mock_event, mock_context)
        
        # Verify result indicates failure with timeout error
        assert result['remediation_status'] == 'failed'
        assert 'error' in result
        assert 'timed out' in result['error'].lower()
        assert '40 seconds' in result['error']
    
    @patch('handler.shutil.copytree')
    @patch('handler.shutil.copy')
    @patch('handler.subprocess.run')
    def test_warm_container_cleanup(
        self, mock_run, mock_copy, mock_copytree, mock_event, mock_context
    ):
        """
        Test: warm container cleanup removes existing /tmp/remediation/.
        
        Verifies that handler cleans up previous execution state.
        """
        # Create existing /tmp/remediation directory (simulating warm container)
        os.makedirs('/tmp/remediation', exist_ok=True)
        with open('/tmp/remediation/old_file.txt', 'w') as f:
            f.write('old content')
        
        # Mock successful tofu apply
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=0,
            stdout="Success",
            stderr=""
        )
        
        # Execute handler
        result = lambda_handler(mock_event, mock_context)
        
        # Verify old file was removed
        assert not os.path.exists('/tmp/remediation/old_file.txt')
        assert result['remediation_status'] == 'success'
    
    @patch('handler.shutil.copytree')
    @patch('handler.shutil.copy')
    @patch('handler.subprocess.run')
    def test_tfvars_contains_correct_values(
        self, mock_run, mock_copy, mock_copytree, mock_context
    ):
        """
        Test: terraform.tfvars.json contains correct bucket_name and region.
        
        Verifies that variables are correctly passed to OpenTofu.
        """
        event = {
            "resource_arn": "arn:aws:s3:::test-bucket-with-dots.example",
            "region": "eu-west-1"
        }
        
        # Mock successful tofu apply
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=0,
            stdout="Success",
            stderr=""
        )
        
        # Execute handler
        lambda_handler(event, mock_context)
        
        # Verify tfvars file contents
        assert os.path.exists('/tmp/terraform.tfvars.json')
        with open('/tmp/terraform.tfvars.json', 'r') as f:
            tfvars = json.load(f)
            assert tfvars['bucket_name'] == 'test-bucket-with-dots.example'
            assert tfvars['region'] == 'eu-west-1'
    
    @patch('handler.shutil.copytree')
    @patch('handler.shutil.copy')
    @patch('handler.subprocess.run')
    def test_subprocess_called_with_refresh_false(
        self, mock_run, mock_copy, mock_copytree, mock_event, mock_context
    ):
        """
        Test: subprocess.run is called with -refresh=false flag.
        
        Requirement 7.3: Execute SHALL use -refresh=false flag.
        """
        # Mock successful tofu apply
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=0,
            stdout="Success",
            stderr=""
        )
        
        # Execute handler
        lambda_handler(mock_event, mock_context)
        
        # Verify -refresh=false is in the command
        call_args = mock_run.call_args[0][0]
        assert '-refresh=false' in call_args
    
    @patch('handler.shutil.copytree')
    @patch('handler.shutil.copy')
    @patch('handler.subprocess.run')
    def test_unexpected_exception_returns_failure(
        self, mock_run, mock_copy, mock_copytree, mock_event, mock_context
    ):
        """
        Test: unexpected exception during remediation returns failure.
        
        Verifies error handling for unexpected errors.
        """
        # Mock unexpected exception
        mock_run.side_effect = Exception("Unexpected error occurred")
        
        # Execute handler
        result = lambda_handler(mock_event, mock_context)
        
        # Verify result indicates failure
        assert result['remediation_status'] == 'failed'
        assert 'error' in result
        assert 'Unexpected error' in result['error']
    
    @patch('handler.shutil.copytree')
    @patch('handler.shutil.copy')
    @patch('handler.subprocess.run')
    def test_multiple_invocations_same_resource_idempotent(
        self, mock_run, mock_copy, mock_copytree, mock_event, mock_context
    ):
        """
        Test: multiple invocations for same resource produce same final state.
        
        Requirement 9.1: Execute SHALL produce the same final resource state when invoked multiple times.
        """
        # Mock successful tofu apply
        mock_run.return_value = CompletedProcess(
            args=['tofu', 'apply'],
            returncode=0,
            stdout="aws_s3_bucket_public_access_block.remediation: Creation complete",
            stderr=""
        )
        
        # Execute handler multiple times
        result1 = lambda_handler(mock_event, mock_context)
        result2 = lambda_handler(mock_event, mock_context)
        result3 = lambda_handler(mock_event, mock_context)
        
        # Verify all invocations return success
        assert result1['remediation_status'] == 'success'
        assert result2['remediation_status'] == 'success'
        assert result3['remediation_status'] == 'success'
        
        # Verify subprocess was called 3 times
        assert mock_run.call_count == 3
