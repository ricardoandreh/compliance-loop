"""
Unit tests for the Analyze Lambda handler (multi-control, fetch_config-driven).

Covers:
- State classification (S_nominal / S_exception / S_violation); a control that
  cannot be evaluated gets no state at all
- Generic resource fetcher: result_path traversal, error_defaults, cache
- OPA evaluation fail-secure behavior
- Exception query: GSI usage, no Limit (regression: Limit=1 + FilterExpression
  could skip a valid exception), ExceptionQueryError on failure
- Exceptions are only queried for non-compliant resources
- Canonical audit timestamp format
"""

import importlib.util
import json
import os
import sys
import uuid
import pytest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'testing')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'testing')
os.environ.setdefault('EXCEPTIONS_TABLE', 'exceptions')

# Load under a unique module name: both lambdas ship a module called
# `handler`, and a plain `import handler` would collide across test files.
_spec = importlib.util.spec_from_file_location(
    'analyze_handler_unit',
    os.path.join(os.path.dirname(__file__), '../../lambdas/analyze/handler.py'),
)
handler = importlib.util.module_from_spec(_spec)
sys.modules['analyze_handler_unit'] = handler
_spec.loader.exec_module(handler)

canonical_timestamp = handler.canonical_timestamp
deserialize_item = handler.deserialize_item
fetch_resource_config = handler.fetch_resource_config
evaluate_policy_with_opa = handler.evaluate_policy_with_opa
query_exception = handler.query_exception
classify_state = handler.classify_state
extract_resource_id = handler.extract_resource_id
lambda_handler = handler.lambda_handler
ExceptionQueryError = handler.ExceptionQueryError
PolicyEvaluationError = handler.PolicyEvaluationError


def typed_control(control_id='ctrl-s3-public-block', rego_package='s3_public_access'):
    """A control item as delivered by the Step Function (DynamoDB typed format)."""
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
        }},
    }


def base_event(controls=None):
    return {
        'resource_arn': 'arn:aws:s3:::my-test-bucket',
        'account_id': '123456789012',
        'region': 'us-east-1',
        'event_time': '2026-01-15T10:30:00Z',
        'controls': controls if controls is not None else [typed_control()],
    }


@pytest.fixture(autouse=True)
def clear_caches():
    handler._config_cache.clear()
    handler._boto_clients.clear()
    yield
    handler._config_cache.clear()
    handler._boto_clients.clear()


class TestCanonicalTimestamp:
    def test_format_is_utc_millis_z(self):
        ts = canonical_timestamp()
        # e.g. 2026-01-15T10:30:00.123Z — fixed width, lexicographically sortable
        assert len(ts) == 24
        assert ts.endswith('Z')
        assert '+00:00' not in ts

    def test_sorts_lexicographically(self):
        a = canonical_timestamp()
        b = canonical_timestamp()
        assert a <= b


class TestDeserializeItem:
    def test_typed_control_roundtrip(self):
        item = deserialize_item(typed_control())
        assert item['control_id'] == 'ctrl-s3-public-block'
        assert item['fetch_config']['method'] == 'get_public_access_block'
        assert item['fetch_config']['params'] == {'Bucket': '{resource_id}'}


class TestClassifyState:
    def test_compliant_is_nominal(self):
        assert classify_state(True, None) == 'S_nominal'

    def test_compliant_with_exception_still_nominal(self):
        assert classify_state(True, {'exception_id': 'exc-1'}) == 'S_nominal'

    def test_noncompliant_with_exception(self):
        assert classify_state(False, {'exception_id': 'exc-1'}) == 'S_exception'

    def test_noncompliant_without_exception(self):
        assert classify_state(False, None) == 'S_violation'


class TestExtractResourceId:
    def test_s3_arn(self):
        assert extract_resource_id('arn:aws:s3:::my-bucket') == 'my-bucket'

    def test_s3_arn_with_dots(self):
        assert extract_resource_id('arn:aws:s3:::my.test.bucket') == 'my.test.bucket'

    def test_empty_s3_arn_raises(self):
        with pytest.raises(ValueError):
            extract_resource_id('arn:aws:s3:::')

    def test_non_s3_arn_takes_last_segment(self):
        assert extract_resource_id('arn:aws:us-east-1:123456789012') == '123456789012'


class TestFetchResourceConfig:
    def fetch_cfg(self, **overrides):
        cfg = {
            'service': 's3',
            'method': 'get_public_access_block',
            'params': {'Bucket': '{resource_id}'},
            'result_path': 'PublicAccessBlockConfiguration',
        }
        cfg.update(overrides)
        return cfg

    def test_happy_path_traverses_result_path(self):
        client = MagicMock()
        client.get_public_access_block.return_value = {
            'PublicAccessBlockConfiguration': {'BlockPublicAcls': True}
        }
        with patch.object(handler, 'get_boto_client', return_value=client):
            result = fetch_resource_config(self.fetch_cfg(), 'my-bucket')
        assert result == {'BlockPublicAcls': True}
        client.get_public_access_block.assert_called_once_with(Bucket='my-bucket')

    def test_error_default_applied_for_known_code(self):
        client = MagicMock()
        client.get_public_access_block.side_effect = ClientError(
            {'Error': {'Code': 'NoSuchPublicAccessBlockConfiguration', 'Message': 'x'}},
            'GetPublicAccessBlock',
        )
        cfg = self.fetch_cfg(error_defaults={
            'NoSuchPublicAccessBlockConfiguration': {'BlockPublicAcls': False}
        })
        with patch.object(handler, 'get_boto_client', return_value=client):
            result = fetch_resource_config(cfg, 'my-bucket')
        assert result == {'BlockPublicAcls': False}

    def test_unknown_error_code_propagates(self):
        client = MagicMock()
        client.get_public_access_block.side_effect = ClientError(
            {'Error': {'Code': 'NoSuchBucket', 'Message': 'x'}},
            'GetPublicAccessBlock',
        )
        cfg = self.fetch_cfg(error_defaults={'SomethingElse': {}})
        with patch.object(handler, 'get_boto_client', return_value=client):
            with pytest.raises(ClientError):
                fetch_resource_config(cfg, 'my-bucket')

    def test_result_cached_per_method_and_resource(self):
        client = MagicMock()
        client.get_public_access_block.return_value = {
            'PublicAccessBlockConfiguration': {'BlockPublicAcls': True}
        }
        with patch.object(handler, 'get_boto_client', return_value=client):
            fetch_resource_config(self.fetch_cfg(), 'my-bucket')
            fetch_resource_config(self.fetch_cfg(), 'my-bucket')
        assert client.get_public_access_block.call_count == 1

    def test_placeholder_substitution_is_structural(self):
        """Regression: replacing {resource_id} on the serialized JSON string
        corrupts the document for ids containing quotes or backslashes."""
        client = MagicMock()
        client.get_public_access_block.return_value = {
            'PublicAccessBlockConfiguration': {'BlockPublicAcls': True}
        }
        weird_id = 'name-with"quote'
        with patch.object(handler, 'get_boto_client', return_value=client):
            fetch_resource_config(self.fetch_cfg(), weird_id)
        client.get_public_access_block.assert_called_once_with(Bucket=weird_id)


class TestEvaluatePolicyWithOpa:
    def completed(self, returncode, stdout='', stderr=''):
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = stderr
        return m

    def opa_output(self, value):
        return json.dumps({'result': [{'expressions': [{'value': value}]}]})

    def test_allow_true(self):
        with patch.object(handler.subprocess, 'run',
                          return_value=self.completed(0, self.opa_output(True))), \
             patch.object(handler.os.path, 'exists', return_value=True):
            assert evaluate_policy_with_opa('s3_public_access', {'x': 1}) is True

    def test_allow_false(self):
        with patch.object(handler.subprocess, 'run',
                          return_value=self.completed(0, self.opa_output(False))), \
             patch.object(handler.os.path, 'exists', return_value=True):
            assert evaluate_policy_with_opa('s3_public_access', {'x': 1}) is False

    def test_opa_error_raises_not_violation(self):
        """Regression: an evaluation FAILURE must not be conflated with
        non-compliance — that would remediate based on no policy decision."""
        with patch.object(handler.subprocess, 'run',
                          return_value=self.completed(1, '', 'rego parse error')), \
             patch.object(handler.os.path, 'exists', return_value=True):
            with pytest.raises(PolicyEvaluationError):
                evaluate_policy_with_opa('s3_public_access', {'x': 1})

    def test_opa_exception_raises_not_violation(self):
        with patch.object(handler.subprocess, 'run', side_effect=OSError('no binary')), \
             patch.object(handler.os.path, 'exists', return_value=True):
            with pytest.raises(PolicyEvaluationError):
                evaluate_policy_with_opa('s3_public_access', {'x': 1})

    def test_datetime_in_resource_config_is_serialized(self):
        """Regression: boto3 responses carry datetime objects (RDS,
        CloudTrail); the OPA input dump must not raise TypeError on them."""
        from datetime import datetime, timezone
        config = {'InstanceCreateTime': datetime.now(timezone.utc)}
        with patch.object(handler.subprocess, 'run',
                          return_value=self.completed(0, self.opa_output(True))), \
             patch.object(handler.os.path, 'exists', return_value=True):
            assert evaluate_policy_with_opa('rds_encryption', config) is True

    def test_cache_path_is_versioned(self):
        """Regression: a warm container must not evaluate a stale .rego after
        the control's policy_version changes."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['policy_path'] = cmd[cmd.index('--data') + 1]
            m = MagicMock()
            m.returncode = 0
            m.stdout = json.dumps({'result': [{'expressions': [{'value': True}]}]})
            return m

        with patch.object(handler.subprocess, 'run', side_effect=fake_run), \
             patch.object(handler.os.path, 'exists', return_value=True):
            evaluate_policy_with_opa('s3_public_access', {'x': 1}, policy_version='v2.3')
        assert captured['policy_path'] == '/tmp/policies/s3_public_access@v2.3.rego'

    def read_opa_input(self):
        with open('/tmp/opa_input.json') as f:
            return json.load(f)

    def test_context_is_injected_into_opa_input(self):
        """ctrl-s3-ssl-only validates that the Deny covers the whole bucket by
        reading input.context.resource_arn. The rego tolerates a missing
        context (older callers), so dropping it here would silently turn that
        coverage check into a no-op rather than fail."""
        ctx = {'resource_id': 'b', 'resource_arn': 'arn:aws:s3:::b',
               'account_id': '1', 'region': 'us-east-1'}
        with patch.object(handler.subprocess, 'run',
                          return_value=self.completed(0, self.opa_output(True))), \
             patch.object(handler.os.path, 'exists', return_value=True):
            evaluate_policy_with_opa('s3_ssl_only', {'x': 1}, context=ctx)
        assert self.read_opa_input() == {'resource': {'x': 1}, 'context': ctx}

    def test_no_context_keeps_legacy_input_shape(self):
        with patch.object(handler.subprocess, 'run',
                          return_value=self.completed(0, self.opa_output(True))), \
             patch.object(handler.os.path, 'exists', return_value=True):
            evaluate_policy_with_opa('s3_ssl_only', {'x': 1})
        assert self.read_opa_input() == {'resource': {'x': 1}}


class TestOpaBudget:
    """The per-control OPA budget must come from the time actually left, so a
    slow control degrades to an unevaluated result instead of killing the whole invocation
    (which would lose the per-control rows for every control)."""

    class Ctx:
        def __init__(self, ms):
            self._ms = ms

        def get_remaining_time_in_millis(self):
            return self._ms

    def test_plenty_of_time_caps_at_max(self):
        assert handler.opa_budget(self.Ctx(60_000)) == handler.OPA_MAX_TIMEOUT

    def test_budget_shrinks_with_remaining_time(self):
        # 4s left minus the 3s reserve leaves 1s.
        assert handler.opa_budget(self.Ctx(4_000)) == pytest.approx(1.0)

    def test_exhausted_budget_is_zero_never_negative(self):
        assert handler.opa_budget(self.Ctx(500)) == 0.0

    def test_no_context_falls_back_to_max(self):
        assert handler.opa_budget(None) == handler.OPA_MAX_TIMEOUT

    def test_zero_budget_refuses_to_start_opa(self):
        """Regression: with no time left, report the control without a state rather than launching
        a subprocess that Lambda will kill mid-flight."""
        with patch.object(handler.subprocess, 'run') as run, \
             patch.object(handler.os.path, 'exists', return_value=True):
            with pytest.raises(PolicyEvaluationError, match='No time budget'):
                evaluate_policy_with_opa('s3_public_access', {'x': 1}, timeout=0)
        run.assert_not_called()

    def test_budget_is_passed_through_to_subprocess(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['timeout'] = kwargs.get('timeout')
            m = MagicMock()
            m.returncode = 0
            m.stdout = json.dumps({'result': [{'expressions': [{'value': True}]}]})
            return m

        with patch.object(handler.subprocess, 'run', side_effect=fake_run), \
             patch.object(handler.os.path, 'exists', return_value=True):
            evaluate_policy_with_opa('s3_public_access', {'x': 1}, timeout=2.5)
        assert captured['timeout'] == 2.5


class TestQueryException:
    def test_returns_active_exception(self):
        table = MagicMock()
        table.query.return_value = {'Items': [{'exception_id': 'exc-1', 'approved_by': 'sec'}]}
        with patch.object(handler, 'exceptions_table', table):
            result = query_exception('ctrl-s3-public-block', 'arn:aws:s3:::b')
        assert result['exception_id'] == 'exc-1'

    def test_returns_none_when_no_items(self):
        table = MagicMock()
        table.query.return_value = {'Items': []}
        with patch.object(handler, 'exceptions_table', table):
            assert query_exception('ctrl-s3-public-block', 'arn:aws:s3:::b') is None

    def test_uses_resource_control_index_without_limit(self):
        """Regression: Limit=1 is applied BEFORE FilterExpression, so it could
        read only an expired exception and miss a valid one for the same pair."""
        table = MagicMock()
        table.query.return_value = {'Items': []}
        with patch.object(handler, 'exceptions_table', table):
            query_exception('ctrl-s3-public-block', 'arn:aws:s3:::b')
        kwargs = table.query.call_args[1]
        assert kwargs['IndexName'] == 'resource-control-index'
        assert kwargs['KeyConditionExpression'] == 'resource_arn = :arn AND control_id = :cid'
        assert kwargs['FilterExpression'] == 'valid_until > :now'
        assert 'Limit' not in kwargs

    def test_query_failure_raises_not_none(self):
        """Regression: a failed query must NOT be treated as 'no exception' —
        that would remediate a resource with a valid approved exception."""
        table = MagicMock()
        table.query.side_effect = RuntimeError('timeout')
        with patch.object(handler, 'exceptions_table', table):
            with pytest.raises(ExceptionQueryError):
                query_exception('ctrl-s3-public-block', 'arn:aws:s3:::b')


class TestLambdaHandler:
    def run_handler(self, event, *, compliant, exception=None, fetch_error=None,
                    query_error=None):
        fetch = MagicMock()
        if fetch_error:
            fetch.side_effect = fetch_error
        else:
            fetch.return_value = {'BlockPublicAcls': compliant}

        query = MagicMock()
        if query_error:
            query.side_effect = query_error
        else:
            query.return_value = exception

        with patch.object(handler, 'fetch_resource_config', fetch), \
             patch.object(handler, 'evaluate_policy_with_opa', return_value=compliant), \
             patch.object(handler, 'query_exception', query):
            result = lambda_handler(event, None)
        return result, fetch, query

    def test_compliant_resource_is_nominal(self):
        result, _, _ = self.run_handler(base_event(), compliant=True)
        assert len(result['results']) == 1
        r = result['results'][0]
        assert r['state'] == 'S_nominal'
        assert r['exception_id'] is None
        assert uuid.UUID(r['violation_id'])
        assert r['policy_version'] == 's3_public_access:v1.0'
        assert r['remediation_s3_key'] == 'controls/ctrl-s3-public-block/remediation.tf'

    def test_handler_passes_evaluation_context_to_opa(self):
        """Regression guard for the call site, not the function: the handler
        must hand OPA the resource identity. Without it ctrl-s3-ssl-only's
        bucket-coverage check silently degrades to a no-op."""
        evaluate = MagicMock(return_value=True)
        with patch.object(handler, 'fetch_resource_config',
                          MagicMock(return_value={'BlockPublicAcls': True})), \
             patch.object(handler, 'evaluate_policy_with_opa', evaluate), \
             patch.object(handler, 'query_exception', MagicMock(return_value=None)):
            lambda_handler(base_event(), None)

        ctx = evaluate.call_args.kwargs['context']
        assert ctx['resource_arn'] == 'arn:aws:s3:::my-test-bucket'
        assert ctx['resource_id'] == 'my-test-bucket'
        assert ctx['account_id'] == '123456789012'
        assert ctx['region'] == 'us-east-1'

    def test_compliant_resource_skips_exception_query(self):
        """Exceptions only matter for non-compliant resources; querying for
        compliant ones would waste reads and risk spurious escalations."""
        _, _, query = self.run_handler(base_event(), compliant=True)
        query.assert_not_called()

    def test_noncompliant_with_exception(self):
        result, _, _ = self.run_handler(
            base_event(), compliant=False,
            exception={'exception_id': 'exc-1', 'approved_by': 'sec-lead'},
        )
        r = result['results'][0]
        assert r['state'] == 'S_exception'
        assert r['exception_id'] == 'exc-1'
        assert r['approved_by'] == 'sec-lead'

    def test_noncompliant_without_exception_is_violation(self):
        result, _, _ = self.run_handler(base_event(), compliant=False)
        r = result['results'][0]
        assert r['state'] == 'S_violation'
        assert r['exception_id'] is None

    def test_fetch_failure_yields_no_state(self):
        result, _, _ = self.run_handler(
            base_event(), compliant=False,
            fetch_error=ClientError({'Error': {'Code': 'NoSuchBucket', 'Message': 'x'}}, 'op'),
        )
        r = result['results'][0]
        assert 'state' not in r
        assert 'NoSuchBucket' in r['error']

    def test_exception_query_failure_yields_no_state(self):
        """Regression: DynamoDB failure during exception lookup escalates
        instead of silently remediating a possibly-authorized resource."""
        result, _, _ = self.run_handler(
            base_event(), compliant=False,
            query_error=ExceptionQueryError('timeout'),
        )
        assert 'state' not in result['results'][0]

    def test_opa_failure_yields_no_state(self):
        """Regression: OPA evaluation failure escalates without a state instead of
        classifying S_violation and remediating an unevaluated resource."""
        with patch.object(handler, 'fetch_resource_config',
                          MagicMock(return_value={'x': 1})), \
             patch.object(handler, 'evaluate_policy_with_opa',
                          side_effect=PolicyEvaluationError('rego broken')), \
             patch.object(handler, 'query_exception', MagicMock(return_value=None)):
            result = lambda_handler(base_event(), None)
        r = result['results'][0]
        assert 'state' not in r
        assert 'rego broken' in r['error']

    def test_every_state_is_one_of_the_three_formal_states(self):
        """The formal model is S_nominal / S_exception / S_violation. A result
        either carries one of them or carries an error and no state."""
        controls = [
            {**typed_control('ctrl-broken'), 'fetch_config': {'M': {}}},
            typed_control('ctrl-s3-public-block'),
        ]
        for compliant in (True, False):
            result, _, _ = self.run_handler(base_event(controls), compliant=compliant)
            for r in result['results']:
                if 'state' in r:
                    assert r['state'] in {'S_nominal', 'S_exception', 'S_violation'}
                    assert 'error' not in r
                else:
                    assert r['error']

    def test_one_result_per_control(self):
        controls = [
            typed_control('ctrl-s3-public-block', 's3_public_access'),
            typed_control('ctrl-s3-versioning', 's3_versioning'),
        ]
        result, _, _ = self.run_handler(base_event(controls), compliant=True)
        assert [r['control_id'] for r in result['results']] == [
            'ctrl-s3-public-block', 'ctrl-s3-versioning'
        ]

    def test_error_in_one_control_does_not_stop_others(self):
        # ctrl-broken has an empty fetch_config → missing keys → no state
        controls = [
            {**typed_control('ctrl-broken'), 'fetch_config': {'M': {}}},
            typed_control('ctrl-s3-public-block'),
        ]
        fetch = MagicMock(return_value={'BlockPublicAcls': True})
        with patch.object(handler, 'fetch_resource_config', fetch), \
             patch.object(handler, 'evaluate_policy_with_opa', return_value=True), \
             patch.object(handler, 'query_exception', MagicMock(return_value=None)):
            result = lambda_handler(base_event(controls), None)
        states = {r['control_id']: r.get('state') for r in result['results']}
        assert states['ctrl-broken'] is None
        assert states['ctrl-s3-public-block'] == 'S_nominal'

    def test_control_without_fetch_config_has_no_state(self):
        control = typed_control()
        del control['fetch_config']
        result, _, _ = self.run_handler(base_event([control]), compliant=True)
        r = result['results'][0]
        assert 'state' not in r
        assert 'fetch_config' in r['error']

    def test_passthrough_fields(self):
        result, _, _ = self.run_handler(base_event(), compliant=True)
        assert result['resource_arn'] == 'arn:aws:s3:::my-test-bucket'
        assert result['account_id'] == '123456789012'
        assert result['region'] == 'us-east-1'
