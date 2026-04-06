"""
Unit tests for Analyze Lambda Handler

Tests cover:
- Compliant resource classification (S_nominal)
- Non-compliant resource with valid exception (S_exception)
- Non-compliant resource without exception (S_violation)
- Expired exception handling
- Error handling for GetPublicAccessBlock
- OPA evaluation errors (fail-secure)
- DynamoDB query timeouts
"""

import json
import os
import subprocess
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, patch, call
from botocore.exceptions import ClientError

# Set environment variables before importing handler
os.environ['EXCEPTIONS_TABLE'] = 'exceptions'
os.environ['VIOLATIONS_LOG_TABLE'] = 'violations_log'

# Import handler after setting environment
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../lambdas/analyze'))
from handler import (
    validate_input,
    extract_bucket_name,
    get_bucket_public_access_config,
    evaluate_policy_with_opa,
    query_exception,
    classify_state,
    log_violation,
    lambda_handler
)


class TestValidateInput(unittest.TestCase):
    """Test input validation from EventBridge Input Transformer."""
    
    def test_valid_input_passes(self):
        """Test that valid input passes validation."""
        event = {
            'resource_arn': 'arn:aws:s3:::my-test-bucket',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T10:30:00Z'
        }
        
        # Should not raise any exception
        validate_input(event)
    
    def test_missing_resource_arn_raises_error(self):
        """Test that missing resource_arn raises ValueError."""
        event = {
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T10:30:00Z'
        }
        
        with self.assertRaises(ValueError) as context:
            validate_input(event)
        
        self.assertIn('resource_arn', str(context.exception))
    
    def test_missing_control_id_raises_error(self):
        """Test that missing control_id raises ValueError."""
        event = {
            'resource_arn': 'arn:aws:s3:::my-test-bucket',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T10:30:00Z'
        }
        
        with self.assertRaises(ValueError) as context:
            validate_input(event)
        
        self.assertIn('control_id', str(context.exception))
    
    def test_missing_account_id_raises_error(self):
        """Test that missing account_id raises ValueError."""
        event = {
            'resource_arn': 'arn:aws:s3:::my-test-bucket',
            'control_id': 'ctrl-s3-public-block',
            'region': 'us-east-1',
            'event_time': '2024-01-15T10:30:00Z'
        }
        
        with self.assertRaises(ValueError) as context:
            validate_input(event)
        
        self.assertIn('account_id', str(context.exception))
    
    def test_missing_region_raises_error(self):
        """Test that missing region raises ValueError."""
        event = {
            'resource_arn': 'arn:aws:s3:::my-test-bucket',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'event_time': '2024-01-15T10:30:00Z'
        }
        
        with self.assertRaises(ValueError) as context:
            validate_input(event)
        
        self.assertIn('region', str(context.exception))
    
    def test_missing_event_time_raises_error(self):
        """Test that missing event_time raises ValueError."""
        event = {
            'resource_arn': 'arn:aws:s3:::my-test-bucket',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1'
        }
        
        with self.assertRaises(ValueError) as context:
            validate_input(event)
        
        self.assertIn('event_time', str(context.exception))
    
    def test_missing_multiple_fields_raises_error(self):
        """Test that missing multiple fields raises ValueError with all missing fields."""
        event = {
            'resource_arn': 'arn:aws:s3:::my-test-bucket'
        }
        
        with self.assertRaises(ValueError) as context:
            validate_input(event)
        
        error_message = str(context.exception)
        self.assertIn('control_id', error_message)
        self.assertIn('account_id', error_message)
        self.assertIn('region', error_message)
        self.assertIn('event_time', error_message)
    
    def test_invalid_resource_arn_format_raises_error(self):
        """Test that invalid resource_arn format raises ValueError."""
        event = {
            'resource_arn': 'invalid-arn-format',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T10:30:00Z'
        }
        
        with self.assertRaises(ValueError) as context:
            validate_input(event)
        
        self.assertIn('Invalid resource_arn format', str(context.exception))
        self.assertIn('arn:aws:s3:::bucket-name', str(context.exception))
    
    def test_resource_arn_wrong_service_raises_error(self):
        """Test that resource_arn for wrong service raises ValueError."""
        event = {
            'resource_arn': 'arn:aws:ec2:us-east-1:123456789012:instance/i-1234567890abcdef0',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T10:30:00Z'
        }
        
        with self.assertRaises(ValueError) as context:
            validate_input(event)
        
        self.assertIn('Invalid resource_arn format', str(context.exception))
    
    def test_empty_bucket_name_raises_error(self):
        """Test that empty bucket name in resource_arn raises ValueError."""
        event = {
            'resource_arn': 'arn:aws:s3:::',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T10:30:00Z'
        }
        
        with self.assertRaises(ValueError) as context:
            validate_input(event)
        
        self.assertIn('bucket name is empty', str(context.exception))
    
    def test_resource_arn_with_bucket_name_passes(self):
        """Test that resource_arn with valid bucket name passes validation."""
        event = {
            'resource_arn': 'arn:aws:s3:::my-bucket-with-dashes-123',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T10:30:00Z'
        }
        
        # Should not raise any exception
        validate_input(event)


class TestExtractBucketName(unittest.TestCase):
    """Test bucket name extraction from ARN."""
    
    def test_valid_arn(self):
        """Test extraction from valid S3 ARN."""
        arn = "arn:aws:s3:::my-test-bucket"
        result = extract_bucket_name(arn)
        self.assertEqual(result, "my-test-bucket")
    
    def test_bucket_with_dots(self):
        """Test extraction from bucket name with dots."""
        arn = "arn:aws:s3:::my.test.bucket"
        result = extract_bucket_name(arn)
        self.assertEqual(result, "my.test.bucket")
    
    def test_invalid_arn_format(self):
        """Test that invalid ARN raises ValueError."""
        with self.assertRaises(ValueError):
            extract_bucket_name("invalid-arn")


class TestGetBucketPublicAccessConfig(unittest.TestCase):
    """Test S3 public access block configuration retrieval."""
    
    @patch('handler.s3_client')
    def test_all_blocks_enabled(self, mock_s3):
        """Test retrieval when all public access blocks are enabled."""
        mock_s3.get_public_access_block.return_value = {
            'PublicAccessBlockConfiguration': {
                'BlockPublicAcls': True,
                'IgnorePublicAcls': True,
                'BlockPublicPolicy': True,
                'RestrictPublicBuckets': True
            }
        }
        
        result = get_bucket_public_access_config('test-bucket')
        
        self.assertEqual(result, {
            'BlockPublicAcls': True,
            'IgnorePublicAcls': True,
            'BlockPublicPolicy': True,
            'RestrictPublicBuckets': True
        })
        mock_s3.get_public_access_block.assert_called_once_with(Bucket='test-bucket')
    
    @patch('handler.s3_client')
    def test_all_blocks_disabled(self, mock_s3):
        """Test retrieval when all public access blocks are disabled."""
        mock_s3.get_public_access_block.return_value = {
            'PublicAccessBlockConfiguration': {
                'BlockPublicAcls': False,
                'IgnorePublicAcls': False,
                'BlockPublicPolicy': False,
                'RestrictPublicBuckets': False
            }
        }
        
        result = get_bucket_public_access_config('test-bucket')
        
        self.assertEqual(result, {
            'BlockPublicAcls': False,
            'IgnorePublicAcls': False,
            'BlockPublicPolicy': False,
            'RestrictPublicBuckets': False
        })
    
    @patch('handler.s3_client')
    def test_no_configuration_exists(self, mock_s3):
        """Test handling when no public access block configuration exists."""
        # Create a proper exception class that mimics boto3 exception
        class NoSuchPublicAccessBlockConfiguration(Exception):
            pass
        
        # Create a mock exceptions object with the exception class
        mock_exceptions = Mock()
        mock_exceptions.NoSuchPublicAccessBlockConfiguration = NoSuchPublicAccessBlockConfiguration
        mock_s3.exceptions = mock_exceptions
        
        # Set the side effect to raise the exception
        mock_s3.get_public_access_block.side_effect = NoSuchPublicAccessBlockConfiguration()
        
        result = get_bucket_public_access_config('test-bucket')
        
        # Should return all False when no configuration exists
        self.assertEqual(result, {
            'BlockPublicAcls': False,
            'IgnorePublicAcls': False,
            'BlockPublicPolicy': False,
            'RestrictPublicBuckets': False
        })


class TestEvaluatePolicyWithOPA(unittest.TestCase):
    """Test OPA policy evaluation."""
    
    @patch('handler.subprocess.run')
    def test_compliant_configuration(self, mock_run):
        """Test OPA evaluation for compliant configuration."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                'result': [{
                    'expressions': [{
                        'value': True
                    }]
                }]
            })
        )
        
        config = {
            'BlockPublicAcls': True,
            'IgnorePublicAcls': True,
            'BlockPublicPolicy': True,
            'RestrictPublicBuckets': True
        }
        
        result = evaluate_policy_with_opa(config)
        
        self.assertTrue(result)
    
    @patch('handler.subprocess.run')
    def test_non_compliant_configuration(self, mock_run):
        """Test OPA evaluation for non-compliant configuration."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                'result': [{
                    'expressions': [{
                        'value': False
                    }]
                }]
            })
        )
        
        config = {
            'BlockPublicAcls': False,
            'IgnorePublicAcls': True,
            'BlockPublicPolicy': True,
            'RestrictPublicBuckets': True
        }
        
        result = evaluate_policy_with_opa(config)
        
        self.assertFalse(result)
    
    @patch('handler.subprocess.run')
    def test_opa_error_returns_false(self, mock_run):
        """Test that OPA evaluation error returns False (fail-secure)."""
        mock_run.return_value = Mock(
            returncode=1,
            stderr="OPA evaluation error"
        )
        
        config = {
            'BlockPublicAcls': True,
            'IgnorePublicAcls': True,
            'BlockPublicPolicy': True,
            'RestrictPublicBuckets': True
        }
        
        result = evaluate_policy_with_opa(config)
        
        # Fail-secure: error should return False (violation)
        self.assertFalse(result)
    
    @patch('handler.subprocess.run')
    def test_opa_timeout_returns_false(self, mock_run):
        """Test that OPA timeout returns False (fail-secure)."""
        mock_run.side_effect = subprocess.TimeoutExpired('opa', 5)
        
        config = {
            'BlockPublicAcls': True,
            'IgnorePublicAcls': True,
            'BlockPublicPolicy': True,
            'RestrictPublicBuckets': True
        }
        
        result = evaluate_policy_with_opa(config)
        
        # Fail-secure: timeout should return False (violation)
        self.assertFalse(result)
    
    @patch('handler.subprocess.run')
    def test_opa_exception_returns_false(self, mock_run):
        """Test that OPA exception returns False (fail-secure)."""
        mock_run.side_effect = Exception("Unexpected error")
        
        config = {
            'BlockPublicAcls': True,
            'IgnorePublicAcls': True,
            'BlockPublicPolicy': True,
            'RestrictPublicBuckets': True
        }
        
        result = evaluate_policy_with_opa(config)
        
        # Fail-secure: exception should return False (violation)
        self.assertFalse(result)


class TestQueryException(unittest.TestCase):
    """Test DynamoDB exception querying."""
    
    @patch('handler.exceptions_table')
    @patch('handler.time.time')
    def test_valid_exception_found(self, mock_time, mock_table):
        """Test finding a valid exception."""
        current_time = 1704067200  # 2024-01-01 00:00:00
        mock_time.return_value = current_time
        
        mock_table.query.return_value = {
            'Items': [{
                'exception_id': 'exc-123',
                'control_id': 'ctrl-s3-public-block',
                'resource_arn': 'arn:aws:s3:::test-bucket',
                'valid_until': current_time + 86400,  # Valid for 1 more day
                'approved_by': 'security-lead@example.com'
            }]
        }
        
        result = query_exception('ctrl-s3-public-block', 'arn:aws:s3:::test-bucket')
        
        self.assertIsNotNone(result)
        self.assertEqual(result['exception_id'], 'exc-123')
        self.assertEqual(result['approved_by'], 'security-lead@example.com')
    
    @patch('handler.exceptions_table')
    @patch('handler.time.time')
    def test_no_exception_found(self, mock_time, mock_table):
        """Test when no exception exists."""
        mock_time.return_value = 1704067200
        mock_table.query.return_value = {'Items': []}
        
        result = query_exception('ctrl-s3-public-block', 'arn:aws:s3:::test-bucket')
        
        self.assertIsNone(result)
    
    @patch('handler.exceptions_table')
    @patch('handler.time.time')
    def test_dynamodb_query_error_returns_none(self, mock_time, mock_table):
        """Test that DynamoDB query error returns None (fail-secure)."""
        mock_time.return_value = 1704067200
        mock_table.query.side_effect = Exception("DynamoDB error")
        
        result = query_exception('ctrl-s3-public-block', 'arn:aws:s3:::test-bucket')
        
        # Fail-secure: error should return None (proceed without exception)
        self.assertIsNone(result)
    
    @patch('handler.exceptions_table')
    @patch('handler.time.time')
    def test_dynamodb_timeout_returns_none(self, mock_time, mock_table):
        """Test that DynamoDB timeout returns None (fail-secure)."""
        mock_time.return_value = 1704067200
        mock_table.query.side_effect = ClientError(
            {'Error': {'Code': 'RequestTimeout'}},
            'Query'
        )
        
        result = query_exception('ctrl-s3-public-block', 'arn:aws:s3:::test-bucket')
        
        # Fail-secure: timeout should return None (proceed without exception)
        self.assertIsNone(result)


class TestClassifyState(unittest.TestCase):
    """Test state classification logic."""
    
    def test_compliant_returns_s_nominal(self):
        """Test that compliant resource returns S_nominal."""
        result = classify_state(is_compliant=True, exception=None)
        self.assertEqual(result, "S_nominal")
    
    def test_compliant_with_exception_returns_s_nominal(self):
        """Test that compliant resource with exception still returns S_nominal."""
        exception = {'exception_id': 'exc-123'}
        result = classify_state(is_compliant=True, exception=exception)
        self.assertEqual(result, "S_nominal")
    
    def test_non_compliant_with_valid_exception_returns_s_exception(self):
        """Test that non-compliant resource with valid exception returns S_exception."""
        exception = {'exception_id': 'exc-123', 'approved_by': 'user@example.com'}
        result = classify_state(is_compliant=False, exception=exception)
        self.assertEqual(result, "S_exception")
    
    def test_non_compliant_without_exception_returns_s_violation(self):
        """Test that non-compliant resource without exception returns S_violation."""
        result = classify_state(is_compliant=False, exception=None)
        self.assertEqual(result, "S_violation")


class TestLogViolation(unittest.TestCase):
    """Test violations_log writing."""
    
    @patch('handler.violations_log_table')
    def test_log_s_nominal(self, mock_table):
        """Test logging S_nominal state."""
        log_violation(
            violation_id='vio-123',
            resource_arn='arn:aws:s3:::test-bucket',
            control_id='ctrl-s3-public-block',
            state='S_nominal',
            account_id='123456789012',
            region='us-east-1',
            exception=None
        )
        
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]['Item']
        
        self.assertEqual(item['violation_id'], 'vio-123')
        self.assertEqual(item['state'], 'S_nominal')
        self.assertNotIn('exception_id', item)
        self.assertNotIn('approved_by', item)
    
    @patch('handler.violations_log_table')
    def test_log_s_exception(self, mock_table):
        """Test logging S_exception state with exception details."""
        exception = {
            'exception_id': 'exc-123',
            'approved_by': 'security-lead@example.com'
        }
        
        log_violation(
            violation_id='vio-456',
            resource_arn='arn:aws:s3:::test-bucket',
            control_id='ctrl-s3-public-block',
            state='S_exception',
            account_id='123456789012',
            region='us-east-1',
            exception=exception
        )
        
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]['Item']
        
        self.assertEqual(item['violation_id'], 'vio-456')
        self.assertEqual(item['state'], 'S_exception')
        self.assertEqual(item['exception_id'], 'exc-123')
        self.assertEqual(item['approved_by'], 'security-lead@example.com')
    
    @patch('handler.violations_log_table')
    def test_log_s_violation(self, mock_table):
        """Test logging S_violation state."""
        log_violation(
            violation_id='vio-789',
            resource_arn='arn:aws:s3:::test-bucket',
            control_id='ctrl-s3-public-block',
            state='S_violation',
            account_id='123456789012',
            region='us-east-1',
            exception=None
        )
        
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]['Item']
        
        self.assertEqual(item['violation_id'], 'vio-789')
        self.assertEqual(item['state'], 'S_violation')
        self.assertNotIn('exception_id', item)


class TestLambdaHandlerCompliantResource(unittest.TestCase):
    """Test lambda_handler for compliant resource (S_nominal)."""
    
    @patch('handler.log_violation')
    @patch('handler.query_exception')
    @patch('handler.evaluate_policy_with_opa')
    @patch('handler.get_bucket_public_access_config')
    @patch('handler.uuid.uuid4')
    def test_compliant_resource_returns_s_nominal(
        self, mock_uuid, mock_get_config, mock_opa, mock_query, mock_log
    ):
        """Test that compliant resource returns S_nominal."""
        # Setup mocks
        mock_uuid.return_value = Mock(hex='550e8400e29b41d4a716446655440000')
        mock_get_config.return_value = {
            'BlockPublicAcls': True,
            'IgnorePublicAcls': True,
            'BlockPublicPolicy': True,
            'RestrictPublicBuckets': True
        }
        mock_opa.return_value = True  # Compliant
        mock_query.return_value = None  # No exception
        
        # Input event
        event = {
            'resource_arn': 'arn:aws:s3:::compliant-bucket',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T10:30:00Z'
        }
        
        # Execute
        result = lambda_handler(event, None)
        
        # Assertions
        self.assertEqual(result['state'], 'S_nominal')
        self.assertIsNotNone(result['violation_id'])
        self.assertIsNone(result['exception_id'])
        self.assertIsNone(result['approved_by'])
        
        # Verify log_violation was called with correct state
        mock_log.assert_called_once()
        self.assertEqual(mock_log.call_args[1]['state'], 'S_nominal')


class TestLambdaHandlerNonCompliantWithValidException(unittest.TestCase):
    """Test lambda_handler for non-compliant resource with valid exception (S_exception)."""
    
    @patch('handler.log_violation')
    @patch('handler.query_exception')
    @patch('handler.evaluate_policy_with_opa')
    @patch('handler.get_bucket_public_access_config')
    @patch('handler.uuid.uuid4')
    def test_non_compliant_with_valid_exception_returns_s_exception(
        self, mock_uuid, mock_get_config, mock_opa, mock_query, mock_log
    ):
        """Test that non-compliant resource with valid exception returns S_exception."""
        # Setup mocks
        mock_uuid.return_value = Mock(hex='660f9511f3ac52e5b827557766551111')
        mock_get_config.return_value = {
            'BlockPublicAcls': False,
            'IgnorePublicAcls': False,
            'BlockPublicPolicy': False,
            'RestrictPublicBuckets': False
        }
        mock_opa.return_value = False  # Non-compliant
        mock_query.return_value = {
            'exception_id': 'exc-abc123',
            'approved_by': 'security-lead@example.com',
            'valid_until': int(time.time()) + 86400
        }
        
        # Input event
        event = {
            'resource_arn': 'arn:aws:s3:::exception-bucket',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T11:00:00Z'
        }
        
        # Execute
        result = lambda_handler(event, None)
        
        # Assertions
        self.assertEqual(result['state'], 'S_exception')
        self.assertIsNotNone(result['violation_id'])
        self.assertEqual(result['exception_id'], 'exc-abc123')
        self.assertEqual(result['approved_by'], 'security-lead@example.com')
        
        # Verify log_violation was called with correct state and exception
        mock_log.assert_called_once()
        self.assertEqual(mock_log.call_args[1]['state'], 'S_exception')
        self.assertIsNotNone(mock_log.call_args[1]['exception'])


class TestLambdaHandlerNonCompliantWithoutException(unittest.TestCase):
    """Test lambda_handler for non-compliant resource without exception (S_violation)."""
    
    @patch('handler.log_violation')
    @patch('handler.query_exception')
    @patch('handler.evaluate_policy_with_opa')
    @patch('handler.get_bucket_public_access_config')
    @patch('handler.uuid.uuid4')
    def test_non_compliant_without_exception_returns_s_violation(
        self, mock_uuid, mock_get_config, mock_opa, mock_query, mock_log
    ):
        """Test that non-compliant resource without exception returns S_violation."""
        # Setup mocks
        mock_uuid.return_value = Mock(hex='770g0622g4bd63f6c938668877662222')
        mock_get_config.return_value = {
            'BlockPublicAcls': False,
            'IgnorePublicAcls': True,
            'BlockPublicPolicy': False,
            'RestrictPublicBuckets': True
        }
        mock_opa.return_value = False  # Non-compliant
        mock_query.return_value = None  # No exception
        
        # Input event
        event = {
            'resource_arn': 'arn:aws:s3:::violation-bucket',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T12:00:00Z'
        }
        
        # Execute
        result = lambda_handler(event, None)
        
        # Assertions
        self.assertEqual(result['state'], 'S_violation')
        self.assertIsNotNone(result['violation_id'])
        self.assertIsNone(result['exception_id'])
        self.assertIsNone(result['approved_by'])
        
        # Verify log_violation was called with correct state
        mock_log.assert_called_once()
        self.assertEqual(mock_log.call_args[1]['state'], 'S_violation')


class TestLambdaHandlerExpiredException(unittest.TestCase):
    """Test lambda_handler ignores expired exceptions."""
    
    @patch('handler.log_violation')
    @patch('handler.query_exception')
    @patch('handler.evaluate_policy_with_opa')
    @patch('handler.get_bucket_public_access_config')
    @patch('handler.uuid.uuid4')
    def test_expired_exception_is_ignored(
        self, mock_uuid, mock_get_config, mock_opa, mock_query, mock_log
    ):
        """Test that expired exception is ignored and resource classified as S_violation."""
        # Setup mocks
        mock_uuid.return_value = Mock(hex='880h1733h5ce74g7d049779988773333')
        mock_get_config.return_value = {
            'BlockPublicAcls': False,
            'IgnorePublicAcls': False,
            'BlockPublicPolicy': False,
            'RestrictPublicBuckets': False
        }
        mock_opa.return_value = False  # Non-compliant
        # query_exception returns None because FilterExpression filters out expired exceptions
        mock_query.return_value = None
        
        # Input event
        event = {
            'resource_arn': 'arn:aws:s3:::expired-exception-bucket',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T13:00:00Z'
        }
        
        # Execute
        result = lambda_handler(event, None)
        
        # Assertions - expired exception should be ignored, resulting in S_violation
        self.assertEqual(result['state'], 'S_violation')
        self.assertIsNone(result['exception_id'])
        
        # Verify query was called (it filters expired exceptions in the query)
        mock_query.assert_called_once_with('ctrl-s3-public-block', 'arn:aws:s3:::expired-exception-bucket')


class TestLambdaHandlerGetPublicAccessBlockError(unittest.TestCase):
    """Test lambda_handler error handling for GetPublicAccessBlock failures."""
    
    @patch('handler.get_bucket_public_access_config')
    def test_get_public_access_block_error_propagates(self, mock_get_config):
        """Test that GetPublicAccessBlock error propagates exception."""
        # Setup mock to raise ClientError
        mock_get_config.side_effect = ClientError(
            {'Error': {'Code': 'NoSuchBucket', 'Message': 'Bucket does not exist'}},
            'GetPublicAccessBlock'
        )
        
        # Input event
        event = {
            'resource_arn': 'arn:aws:s3:::nonexistent-bucket',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T14:00:00Z'
        }
        
        # Execute and verify exception is raised
        with self.assertRaises(ClientError) as context:
            lambda_handler(event, None)
        
        self.assertEqual(context.exception.response['Error']['Code'], 'NoSuchBucket')


class TestLambdaHandlerOPAError(unittest.TestCase):
    """Test lambda_handler fail-secure behavior for OPA errors."""
    
    @patch('handler.log_violation')
    @patch('handler.query_exception')
    @patch('handler.evaluate_policy_with_opa')
    @patch('handler.get_bucket_public_access_config')
    @patch('handler.uuid.uuid4')
    def test_opa_error_returns_s_violation(
        self, mock_uuid, mock_get_config, mock_opa, mock_query, mock_log
    ):
        """Test that OPA error results in S_violation (fail-secure)."""
        # Setup mocks
        mock_uuid.return_value = Mock(hex='990i2844i6df85h8e150880099884444')
        mock_get_config.return_value = {
            'BlockPublicAcls': True,
            'IgnorePublicAcls': True,
            'BlockPublicPolicy': True,
            'RestrictPublicBuckets': True
        }
        # OPA returns False on error (fail-secure)
        mock_opa.return_value = False
        mock_query.return_value = None
        
        # Input event
        event = {
            'resource_arn': 'arn:aws:s3:::opa-error-bucket',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T15:00:00Z'
        }
        
        # Execute
        result = lambda_handler(event, None)
        
        # Assertions - OPA error should result in S_violation (fail-secure)
        self.assertEqual(result['state'], 'S_violation')
        
        # Verify log_violation was called with S_violation
        mock_log.assert_called_once()
        self.assertEqual(mock_log.call_args[1]['state'], 'S_violation')


class TestLambdaHandlerDynamoDBTimeout(unittest.TestCase):
    """Test lambda_handler fail-secure behavior for DynamoDB timeouts."""
    
    @patch('handler.log_violation')
    @patch('handler.query_exception')
    @patch('handler.evaluate_policy_with_opa')
    @patch('handler.get_bucket_public_access_config')
    @patch('handler.uuid.uuid4')
    def test_dynamodb_timeout_proceeds_without_exception(
        self, mock_uuid, mock_get_config, mock_opa, mock_query, mock_log
    ):
        """Test that DynamoDB timeout proceeds without exception (fail-secure)."""
        # Setup mocks
        mock_uuid.return_value = Mock(hex='aa0j3955j7eg96i9f261991100995555')
        mock_get_config.return_value = {
            'BlockPublicAcls': False,
            'IgnorePublicAcls': False,
            'BlockPublicPolicy': False,
            'RestrictPublicBuckets': False
        }
        mock_opa.return_value = False  # Non-compliant
        # query_exception returns None on timeout (fail-secure)
        mock_query.return_value = None
        
        # Input event
        event = {
            'resource_arn': 'arn:aws:s3:::dynamodb-timeout-bucket',
            'control_id': 'ctrl-s3-public-block',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'event_time': '2024-01-15T16:00:00Z'
        }
        
        # Execute
        result = lambda_handler(event, None)
        
        # Assertions - DynamoDB timeout should proceed without exception, resulting in S_violation
        self.assertEqual(result['state'], 'S_violation')
        self.assertIsNone(result['exception_id'])
        
        # Verify query was called
        mock_query.assert_called_once()


if __name__ == '__main__':
    unittest.main()
