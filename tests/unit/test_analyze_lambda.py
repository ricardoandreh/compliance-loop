"""
Unit tests for the Analyze Lambda handler (current architecture).

The Analyze Lambda receives pre-fetched control definitions from the Step
Function (QueryControls / GetControl), evaluates each one via OPA against a
generically fetched resource config, and classifies state. It does NOT read the
controls table, validate EventBridge input, or write violations_log (the state
machine does that).

Covered surface:
- extract_resource_id       — ARN -> resource id (S3 and generic)
- canonical_timestamp       — audit-safe ISO-8601 (UTC, ms, 'Z')
- deserialize_item          — DynamoDB typed item -> plain dict
- classify_state            — S_nominal / S_exception / S_violation
- fetch_resource_config     — generic boto3 caller (result_path, error_defaults, cache)
- evaluate_policy_with_opa  — OPA subprocess, fail-secure on error/timeout
- query_exception           — point query on resource-control-index, ExceptionQueryError
- lambda_handler            — multi-control orchestration, per-control S_error isolation
"""

import json
import os
import subprocess
import sys
import time
import unittest
import importlib.util
from unittest.mock import MagicMock, Mock, patch

from botocore.exceptions import ClientError

# Env must be set before the handler module is imported (it creates boto3
# clients and reads table/bucket names at module load).
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ['EXCEPTIONS_TABLE'] = 'exceptions'
os.environ['POLICY_BUCKET'] = ''

# Load the analyze handler by explicit path under a UNIQUE module name.
# Both lambdas/analyze/ and lambdas/execute/ define handler.py, and the property
# test suite loads analyze/handler.py as 'analyze_handler'. If this file reused
# either name, whichever module-level loader ran last during pytest collection
# would win sys.modules[...], and @patch('<name>.x') here would patch the wrong
# module object (silent patch misses -> real AWS calls). A dedicated name keeps
# this suite's patches bound to the module object it actually invokes.
_ANALYZE_HANDLER_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../../lambdas/analyze/handler.py')
)
sys.modules.pop('analyze_handler_unit', None)
_spec = importlib.util.spec_from_file_location('analyze_handler_unit', _ANALYZE_HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
sys.modules['analyze_handler_unit'] = handler
_spec.loader.exec_module(handler)


def _control_item(control_id='ctrl-s3-public-block', rego_package='s3_public_access'):
    """A control definition in DynamoDB-typed format, as the state machine passes it."""
    return {
        'control_id': {'S': control_id},
        'rego_policy_package': {'S': rego_package},
        'policy_version': {'S': 'v1.0'},
        'rego_s3_key': {'S': f'controls/{control_id}/policy.rego'},
        'remediation_s3_key': {'S': f'controls/{control_id}/remediation.tf'},
        'fetch_config': {'M': {
            'service': {'S': 's3'},
            'method': {'S': 'get_public_access_block'},
            'params': {'M': {'Bucket': {'S': '{resource_id}'}}},
            'result_path': {'S': 'PublicAccessBlockConfiguration'},
            'error_defaults': {'M': {}},
        }},
    }


class TestExtractResourceId(unittest.TestCase):
    def test_s3_arn_returns_bucket_name(self):
        self.assertEqual(
            handler.extract_resource_id('arn:aws:s3:::my-test-bucket'),
            'my-test-bucket',
        )

    def test_s3_arn_with_dots(self):
        self.assertEqual(
            handler.extract_resource_id('arn:aws:s3:::my.test.bucket'),
            'my.test.bucket',
        )

    def test_empty_bucket_name_raises(self):
        with self.assertRaises(ValueError):
            handler.extract_resource_id('arn:aws:s3:::')

    def test_generic_arn_returns_last_segment(self):
        # EC2-style ARN -> resource id after the last ':' and '/'
        self.assertEqual(
            handler.extract_resource_id(
                'arn:aws:ec2:us-east-1:123456789012:volume/vol-0abcd1234'
            ),
            'vol-0abcd1234',
        )

    def test_account_level_pseudo_arn(self):
        # EBS-encryption-by-default style account/region pseudo-ARN
        self.assertEqual(
            handler.extract_resource_id('arn:aws:us-east-1:123456789012'),
            '123456789012',
        )


class TestCanonicalTimestamp(unittest.TestCase):
    def test_format_is_utc_millis_with_z(self):
        ts = handler.canonical_timestamp()
        # e.g. 2026-06-28T07:04:45.575Z  -> exactly 24 chars
        self.assertTrue(ts.endswith('Z'))
        self.assertNotIn('+', ts)
        self.assertEqual(len(ts), len('2026-06-28T07:04:45.575Z'))
        self.assertEqual(ts[10], 'T')
        self.assertEqual(ts[19], '.')

    def test_lexicographic_equals_chronological(self):
        a = handler.canonical_timestamp()
        time.sleep(0.005)
        b = handler.canonical_timestamp()
        self.assertLess(a, b)


class TestDeserializeItem(unittest.TestCase):
    def test_deserializes_typed_scalars(self):
        item = {'control_id': {'S': 'ctrl-x'}, 'enabled': {'BOOL': True}}
        result = handler.deserialize_item(item)
        self.assertEqual(result['control_id'], 'ctrl-x')
        self.assertIs(result['enabled'], True)

    def test_deserializes_nested_map(self):
        item = _control_item()
        result = handler.deserialize_item(item)
        self.assertEqual(result['fetch_config']['method'], 'get_public_access_block')
        self.assertEqual(result['fetch_config']['params']['Bucket'], '{resource_id}')


class TestClassifyState(unittest.TestCase):
    def test_compliant_returns_nominal(self):
        self.assertEqual(handler.classify_state(True, None), 'S_nominal')

    def test_compliant_with_exception_still_nominal(self):
        self.assertEqual(
            handler.classify_state(True, {'exception_id': 'exc-1'}), 'S_nominal'
        )

    def test_non_compliant_with_exception_returns_exception(self):
        self.assertEqual(
            handler.classify_state(False, {'exception_id': 'exc-1'}), 'S_exception'
        )

    def test_non_compliant_without_exception_returns_violation(self):
        self.assertEqual(handler.classify_state(False, None), 'S_violation')


class TestFetchResourceConfig(unittest.TestCase):
    def setUp(self):
        handler._config_cache.clear()

    def _fetch_config(self, error_defaults=None):
        return {
            'service': 's3',
            'method': 'get_public_access_block',
            'params': {'Bucket': '{resource_id}'},
            'result_path': 'PublicAccessBlockConfiguration',
            'error_defaults': error_defaults or {},
        }

    @patch('analyze_handler_unit.get_boto_client')
    def test_success_navigates_result_path_and_substitutes_id(self, mock_get_client):
        client = MagicMock()
        client.get_public_access_block.return_value = {
            'PublicAccessBlockConfiguration': {'BlockPublicAcls': True}
        }
        mock_get_client.return_value = client

        result = handler.fetch_resource_config(self._fetch_config(), 'my-bucket')

        self.assertEqual(result, {'BlockPublicAcls': True})
        client.get_public_access_block.assert_called_once_with(Bucket='my-bucket')

    @patch('analyze_handler_unit.get_boto_client')
    def test_error_code_maps_to_default(self, mock_get_client):
        client = MagicMock()
        client.get_public_access_block.side_effect = ClientError(
            {'Error': {'Code': 'NoSuchPublicAccessBlockConfiguration'}},
            'GetPublicAccessBlock',
        )
        mock_get_client.return_value = client

        defaults = {
            'NoSuchPublicAccessBlockConfiguration': {
                'BlockPublicAcls': False,
                'IgnorePublicAcls': False,
                'BlockPublicPolicy': False,
                'RestrictPublicBuckets': False,
            }
        }
        result = handler.fetch_resource_config(
            self._fetch_config(error_defaults=defaults), 'no-config-bucket'
        )
        self.assertEqual(result['BlockPublicAcls'], False)

    @patch('analyze_handler_unit.get_boto_client')
    def test_unmapped_error_reraises(self, mock_get_client):
        client = MagicMock()
        client.get_public_access_block.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied'}}, 'GetPublicAccessBlock'
        )
        mock_get_client.return_value = client

        with self.assertRaises(ClientError):
            handler.fetch_resource_config(self._fetch_config(), 'denied-bucket')

    @patch('analyze_handler_unit.get_boto_client')
    def test_result_is_cached_by_method_and_resource(self, mock_get_client):
        client = MagicMock()
        client.get_public_access_block.return_value = {
            'PublicAccessBlockConfiguration': {'BlockPublicAcls': True}
        }
        mock_get_client.return_value = client

        cfg = self._fetch_config()
        handler.fetch_resource_config(cfg, 'cached-bucket')
        handler.fetch_resource_config(cfg, 'cached-bucket')

        # Second call served from cache: underlying API hit only once.
        client.get_public_access_block.assert_called_once()


class TestEvaluatePolicyWithOPA(unittest.TestCase):
    def _opa_stdout(self, value):
        return json.dumps({'result': [{'expressions': [{'value': value}]}]})

    @patch('analyze_handler_unit.subprocess.run')
    def test_compliant_true(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout=self._opa_stdout(True))
        self.assertTrue(
            handler.evaluate_policy_with_opa('s3_public_access', {'x': 1}, None)
        )

    @patch('analyze_handler_unit.subprocess.run')
    def test_non_compliant_false(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout=self._opa_stdout(False))
        self.assertFalse(
            handler.evaluate_policy_with_opa('s3_public_access', {'x': 1}, None)
        )

    @patch('analyze_handler_unit.subprocess.run')
    def test_opa_nonzero_returncode_is_fail_secure(self, mock_run):
        mock_run.return_value = Mock(returncode=1, stderr='boom', stdout='')
        self.assertFalse(
            handler.evaluate_policy_with_opa('s3_public_access', {'x': 1}, None)
        )

    @patch('analyze_handler_unit.subprocess.run')
    def test_opa_timeout_is_fail_secure(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired('opa', 5)
        self.assertFalse(
            handler.evaluate_policy_with_opa('s3_public_access', {'x': 1}, None)
        )

    @patch('analyze_handler_unit.subprocess.run')
    def test_opa_unexpected_exception_is_fail_secure(self, mock_run):
        mock_run.side_effect = Exception('unexpected')
        self.assertFalse(
            handler.evaluate_policy_with_opa('s3_public_access', {'x': 1}, None)
        )


class TestQueryException(unittest.TestCase):
    @patch('analyze_handler_unit.exceptions_table')
    @patch('analyze_handler_unit.time.time')
    def test_valid_exception_found(self, mock_time, mock_table):
        mock_time.return_value = 1704067200
        mock_table.query.return_value = {
            'Items': [{
                'exception_id': 'exc-123',
                'approved_by': 'security-lead@example.com',
                'valid_until': 1704067200 + 86400,
            }]
        }
        result = handler.query_exception('ctrl-s3-public-block', 'arn:aws:s3:::b')
        self.assertEqual(result['exception_id'], 'exc-123')

    @patch('analyze_handler_unit.exceptions_table')
    @patch('analyze_handler_unit.time.time')
    def test_uses_point_query_on_resource_control_index(self, mock_time, mock_table):
        mock_time.return_value = 1704067200
        mock_table.query.return_value = {'Items': []}

        handler.query_exception('ctrl-s3-public-block', 'arn:aws:s3:::b')

        kwargs = mock_table.query.call_args[1]
        self.assertEqual(kwargs['IndexName'], 'resource-control-index')
        self.assertEqual(
            kwargs['KeyConditionExpression'],
            'resource_arn = :arn AND control_id = :cid',
        )
        self.assertEqual(kwargs['FilterExpression'], 'valid_until > :now')
        # Residual of bug 2: no Limit, so an expired item can't mask a valid one.
        self.assertNotIn('Limit', kwargs)
        self.assertEqual(kwargs['ExpressionAttributeValues'][':arn'], 'arn:aws:s3:::b')
        self.assertEqual(
            kwargs['ExpressionAttributeValues'][':cid'], 'ctrl-s3-public-block'
        )

    @patch('analyze_handler_unit.exceptions_table')
    @patch('analyze_handler_unit.time.time')
    def test_no_exception_returns_none(self, mock_time, mock_table):
        mock_time.return_value = 1704067200
        mock_table.query.return_value = {'Items': []}
        self.assertIsNone(
            handler.query_exception('ctrl-s3-public-block', 'arn:aws:s3:::b')
        )

    @patch('analyze_handler_unit.exceptions_table')
    @patch('analyze_handler_unit.time.time')
    def test_query_failure_raises_exception_query_error(self, mock_time, mock_table):
        mock_time.return_value = 1704067200
        mock_table.query.side_effect = ClientError(
            {'Error': {'Code': 'ProvisionedThroughputExceededException'}}, 'Query'
        )
        # Must NOT be swallowed as "no exception" — that would remediate a
        # possibly-authorized resource. Caller escalates on this error.
        with self.assertRaises(handler.ExceptionQueryError):
            handler.query_exception('ctrl-s3-public-block', 'arn:aws:s3:::b')


class TestLambdaHandler(unittest.TestCase):
    def _event(self, controls=None):
        return {
            'resource_arn': 'arn:aws:s3:::my-bucket',
            'account_id': '123456789012',
            'region': 'us-east-1',
            'controls': controls if controls is not None else [_control_item()],
        }

    @patch('analyze_handler_unit.query_exception')
    @patch('analyze_handler_unit.evaluate_policy_with_opa')
    @patch('analyze_handler_unit.fetch_resource_config')
    def test_compliant_is_nominal_and_skips_exception_query(
        self, mock_fetch, mock_opa, mock_query
    ):
        mock_fetch.return_value = {'BlockPublicAcls': True}
        mock_opa.return_value = True

        out = handler.lambda_handler(self._event(), None)
        result = out['results'][0]

        self.assertEqual(result['state'], 'S_nominal')
        self.assertIsNone(result['exception_id'])
        # Exceptions are only queried for non-compliant resources.
        mock_query.assert_not_called()
        # Timestamp is the canonical audit form.
        self.assertTrue(result['timestamp'].endswith('Z'))

    @patch('analyze_handler_unit.query_exception')
    @patch('analyze_handler_unit.evaluate_policy_with_opa')
    @patch('analyze_handler_unit.fetch_resource_config')
    def test_non_compliant_with_valid_exception_is_exception(
        self, mock_fetch, mock_opa, mock_query
    ):
        mock_fetch.return_value = {'BlockPublicAcls': False}
        mock_opa.return_value = False
        mock_query.return_value = {
            'exception_id': 'exc-abc',
            'approved_by': 'lead@example.com',
        }

        result = handler.lambda_handler(self._event(), None)['results'][0]

        self.assertEqual(result['state'], 'S_exception')
        self.assertEqual(result['exception_id'], 'exc-abc')
        self.assertEqual(result['approved_by'], 'lead@example.com')
        mock_query.assert_called_once_with('ctrl-s3-public-block', 'arn:aws:s3:::my-bucket')

    @patch('analyze_handler_unit.query_exception')
    @patch('analyze_handler_unit.evaluate_policy_with_opa')
    @patch('analyze_handler_unit.fetch_resource_config')
    def test_non_compliant_without_exception_is_violation(
        self, mock_fetch, mock_opa, mock_query
    ):
        mock_fetch.return_value = {'BlockPublicAcls': False}
        mock_opa.return_value = False
        mock_query.return_value = None

        result = handler.lambda_handler(self._event(), None)['results'][0]

        self.assertEqual(result['state'], 'S_violation')
        self.assertIsNone(result['exception_id'])
        self.assertEqual(
            result['remediation_s3_key'],
            'controls/ctrl-s3-public-block/remediation.tf',
        )

    @patch('analyze_handler_unit.query_exception')
    @patch('analyze_handler_unit.evaluate_policy_with_opa')
    @patch('analyze_handler_unit.fetch_resource_config')
    def test_fetch_error_is_isolated_as_s_error(self, mock_fetch, mock_opa, mock_query):
        mock_fetch.side_effect = ClientError(
            {'Error': {'Code': 'NoSuchBucket'}}, 'GetPublicAccessBlock'
        )

        result = handler.lambda_handler(self._event(), None)['results'][0]

        self.assertEqual(result['state'], 'S_error')
        self.assertIn('error', result)
        mock_opa.assert_not_called()

    @patch('analyze_handler_unit.query_exception')
    @patch('analyze_handler_unit.evaluate_policy_with_opa')
    @patch('analyze_handler_unit.fetch_resource_config')
    def test_exception_query_error_becomes_s_error_not_violation(
        self, mock_fetch, mock_opa, mock_query
    ):
        mock_fetch.return_value = {'BlockPublicAcls': False}
        mock_opa.return_value = False
        mock_query.side_effect = handler.ExceptionQueryError('ddb down')

        result = handler.lambda_handler(self._event(), None)['results'][0]

        # Fail-secure for authorized resources: a failed exception lookup must
        # escalate (S_error), never fall through to S_violation -> remediation.
        self.assertEqual(result['state'], 'S_error')

    @patch('analyze_handler_unit.query_exception')
    @patch('analyze_handler_unit.evaluate_policy_with_opa')
    @patch('analyze_handler_unit.fetch_resource_config')
    def test_multiple_controls_are_evaluated_independently(
        self, mock_fetch, mock_opa, mock_query
    ):
        mock_fetch.return_value = {'BlockPublicAcls': False}
        # First control compliant, second not.
        mock_opa.side_effect = [True, False]
        mock_query.return_value = None

        controls = [
            _control_item('ctrl-s3-public-block', 's3_public_access'),
            _control_item('ctrl-s3-versioning', 's3_versioning'),
        ]
        out = handler.lambda_handler(self._event(controls), None)

        self.assertEqual(len(out['results']), 2)
        self.assertEqual(out['results'][0]['state'], 'S_nominal')
        self.assertEqual(out['results'][1]['state'], 'S_violation')
        self.assertEqual(out['resource_arn'], 'arn:aws:s3:::my-bucket')


if __name__ == '__main__':
    unittest.main()
