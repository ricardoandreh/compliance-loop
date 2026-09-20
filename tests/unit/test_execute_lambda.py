"""
Unit tests for the Execute Lambda handler (zip + tofu layer + S3 templates).

Covers:
- Remediation success / failure / timeout paths and OpenTofu invocation flags
- Setup failures (provider cache, template download) returning structured errors
- Clean work_dir per invocation (regression: a stale tfstate from a warm
  container + -refresh=false made tofu report 'No changes' for a re-violated
  resource, skipping remediation while reporting success)
- ssl-only edge case: existing bucket policy passed for merge
- Bucket name resolution from ARN vs resource_id fallback
- Per-violation session policy: the remediation runs with credentials scoped to
  the resource that was classified, and to the actions its control declares
"""

import importlib.util
import json
import os
import shutil
import sys
import uuid
import pytest
from unittest.mock import MagicMock, patch
from subprocess import TimeoutExpired

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'testing')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'testing')
os.environ.setdefault('REMEDIATION_BUCKET', 'test-remediation-bucket')
os.environ.setdefault('REMEDIATION_ROLE_ARN',
                      'arn:aws:iam::123456789012:role/compliance-loop-remediation-dev')

# Load under a unique module name: both lambdas ship a module called
# `handler`, and a plain `import handler` would collide across test files.
_spec = importlib.util.spec_from_file_location(
    'execute_handler_unit',
    os.path.join(os.path.dirname(__file__), '../../lambdas/execute/handler.py'),
)
handler = importlib.util.module_from_spec(_spec)
sys.modules['execute_handler_unit'] = handler
_spec.loader.exec_module(handler)

lambda_handler = handler.lambda_handler
setup_remediation = handler.setup_remediation


def base_event(control_id='ctrl-s3-public-block', **overrides):
    event = {
        'resource_arn': 'arn:aws:s3:::my-test-bucket',
        'resource_id': 'my-test-bucket',
        'control_id': control_id,
        'region': 'us-east-1',
        'violation_id': 'vio-123',
        'remediation_s3_key': f'controls/{control_id}/remediation.tf',
        'remediation_actions': ['s3:PutBucketPublicAccessBlock', 's3:GetBucketPublicAccessBlock'],
        'remediation_scope': 'resource',
    }
    event.update(overrides)
    return event


CREDENTIALS = {'Credentials': {'AccessKeyId': 'AKIA-SCOPED',
                               'SecretAccessKey': 'secret', 'SessionToken': 'token'}}


@pytest.fixture(autouse=True)
def scoped_session():
    """Every remediation assumes the remediation role first.

    The scoped client is the same mock the tests already configure as
    handler.s3, so the assertions about the bucket policy read still hold —
    what changes is which credentials make the call.
    """
    session = MagicMock()
    session.client.side_effect = lambda *a, **k: handler.s3
    with patch.object(handler.sts, 'assume_role', return_value=CREDENTIALS) as assume, \
         patch.object(handler.boto3, 'Session', return_value=session):
        yield assume


def completed(returncode, stdout='', stderr=''):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


@pytest.fixture
def work_dir(tmp_path):
    d = tmp_path / 'work'
    d.mkdir()
    return str(d)


@pytest.fixture
def patched_setup(work_dir):
    """Bypass S3/provider setup; hand the handler a ready work_dir."""
    with patch.object(handler, 'ensure_provider_cache'), \
         patch.object(handler, 'setup_remediation', return_value=work_dir) as setup:
        yield setup


class TestLambdaHandlerSuccess:
    def test_success_returns_output(self, patched_setup):
        with patch.object(handler.subprocess, 'run',
                          return_value=completed(0, 'Apply complete!')) as run:
            result = lambda_handler(base_event(), None)
        assert result == {'remediation_status': 'success', 'output': 'Apply complete!'}
        run.assert_called_once()

    def test_tofu_invocation_flags(self, patched_setup, work_dir):
        with patch.object(handler.subprocess, 'run', return_value=completed(0)) as run:
            lambda_handler(base_event(), None)
        args, kwargs = run.call_args
        cmd = args[0]
        assert cmd[0] == '/opt/bin/tofu'
        assert 'apply' in cmd
        assert '-refresh=false' in cmd
        assert '-auto-approve' in cmd
        assert f'-state=/tmp/ctrl-s3-public-block/my-test-bucket.tfstate' in cmd
        assert kwargs['cwd'] == work_dir
        assert kwargs['timeout'] == 65

    def test_tfvars_written_with_bucket_and_region(self, patched_setup, work_dir):
        with patch.object(handler.subprocess, 'run', return_value=completed(0)):
            lambda_handler(base_event(), None)
        with open(f'{work_dir}/terraform.tfvars.json') as f:
            tfvars = json.load(f)
        assert tfvars['bucket_name'] == 'my-test-bucket'
        assert tfvars['region'] == 'us-east-1'
        assert tfvars['resource_id'] == 'my-test-bucket'

    def test_non_s3_arn_falls_back_to_resource_id(self, patched_setup, work_dir):
        event = base_event(
            control_id='ctrl-ebs-encryption-default',
            resource_arn='arn:aws:us-east-1:123456789012',
            resource_id='account-level',
            remediation_s3_key='controls/ctrl-ebs-encryption-default/remediation.tf',
        )
        with patch.object(handler.subprocess, 'run', return_value=completed(0)):
            lambda_handler(event, None)
        with open(f'{work_dir}/terraform.tfvars.json') as f:
            tfvars = json.load(f)
        assert tfvars['bucket_name'] == 'account-level'


class TestLambdaHandlerFailure:
    def test_nonzero_exit_returns_failed_with_stderr(self, patched_setup):
        with patch.object(handler.subprocess, 'run',
                          return_value=completed(1, '', 'AccessDenied: nope')):
            result = lambda_handler(base_event(), None)
        assert result['remediation_status'] == 'failed'
        assert 'AccessDenied' in result['error']

    def test_timeout_returns_failed(self, patched_setup):
        with patch.object(handler.subprocess, 'run',
                          side_effect=TimeoutExpired('tofu', 65)):
            result = lambda_handler(base_event(), None)
        assert result['remediation_status'] == 'failed'
        assert 'timed out' in result['error']

    def test_setup_failure_returns_failed_without_running_tofu(self, work_dir):
        with patch.object(handler, 'ensure_provider_cache'), \
             patch.object(handler, 'setup_remediation',
                          side_effect=RuntimeError('S3 download failed')), \
             patch.object(handler.subprocess, 'run') as run:
            result = lambda_handler(base_event(), None)
        assert result['remediation_status'] == 'failed'
        assert 'Setup failed' in result['error']
        run.assert_not_called()

    def test_provider_cache_failure_returns_failed(self):
        with patch.object(handler, 'ensure_provider_cache',
                          side_effect=RuntimeError('no cache')), \
             patch.object(handler.subprocess, 'run') as run:
            result = lambda_handler(base_event(), None)
        assert result['remediation_status'] == 'failed'
        assert 'Setup failed' in result['error']
        run.assert_not_called()


class TestSslOnlyPolicyMerge:
    def test_existing_policy_passed_to_tfvars(self, patched_setup, work_dir):
        policy = json.dumps({'Version': '2012-10-17', 'Statement': [{'Sid': 'Keep'}]})
        s3 = MagicMock()
        s3.get_bucket_policy.return_value = {'Policy': policy}
        event = base_event(control_id='ctrl-s3-ssl-only',
                           remediation_s3_key='controls/ctrl-s3-ssl-only/remediation.tf')
        with patch.object(handler, 's3', s3), \
             patch.object(handler.subprocess, 'run', return_value=completed(0)):
            lambda_handler(event, None)
        with open(f'{work_dir}/terraform.tfvars.json') as f:
            tfvars = json.load(f)
        assert tfvars['existing_policy'] == policy

    def test_missing_policy_is_tolerated(self, patched_setup, work_dir):
        from botocore.exceptions import ClientError
        s3 = MagicMock()
        s3.get_bucket_policy.side_effect = ClientError(
            {'Error': {'Code': 'NoSuchBucketPolicy', 'Message': 'x'}}, 'GetBucketPolicy')
        event = base_event(control_id='ctrl-s3-ssl-only',
                           remediation_s3_key='controls/ctrl-s3-ssl-only/remediation.tf')
        with patch.object(handler, 's3', s3), \
             patch.object(handler.subprocess, 'run', return_value=completed(0)):
            result = lambda_handler(event, None)
        assert result['remediation_status'] == 'success'
        with open(f'{work_dir}/terraform.tfvars.json') as f:
            tfvars = json.load(f)
        assert 'existing_policy' not in tfvars

    def test_policy_read_error_aborts_instead_of_overwriting(self, patched_setup):
        """Regression: AccessDenied/throttling on GetBucketPolicy must abort —
        proceeding would rewrite the bucket policy with only the SSL statement,
        silently destroying the customer's statements."""
        from botocore.exceptions import ClientError
        s3 = MagicMock()
        s3.get_bucket_policy.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'nope'}}, 'GetBucketPolicy')
        event = base_event(control_id='ctrl-s3-ssl-only',
                           remediation_s3_key='controls/ctrl-s3-ssl-only/remediation.tf')
        with patch.object(handler, 's3', s3), \
             patch.object(handler.subprocess, 'run', return_value=completed(0)) as run:
            result = lambda_handler(event, None)
        assert result['remediation_status'] == 'failed'
        assert 'AccessDenied' in result['error']
        run.assert_not_called()

    def test_other_controls_do_not_fetch_bucket_policy(self, patched_setup):
        s3 = MagicMock()
        with patch.object(handler, 's3', s3), \
             patch.object(handler.subprocess, 'run', return_value=completed(0)):
            lambda_handler(base_event(), None)
        s3.get_bucket_policy.assert_not_called()


class TestSetupRemediationCleanWorkdir:
    """Regression for the stale-state bug: setup_remediation must recreate the
    work_dir from scratch so no tfstate survives a warm invocation."""

    @pytest.fixture
    def env(self, tmp_path):
        tf_cache = tmp_path / 'tf-dot-terraform'
        tf_cache.mkdir()
        tf_lock = tmp_path / 'tf-lock.hcl'
        tf_lock.write_text('# lock')
        control_id = f'test-{uuid.uuid4().hex[:8]}'
        yield {'tf_cache': str(tf_cache), 'tf_lock': str(tf_lock),
               'control_id': control_id}
        shutil.rmtree(f'/tmp/{control_id}', ignore_errors=True)

    def test_stale_tfstate_is_removed(self, env):
        control_id = env['control_id']
        work_dir = f'/tmp/{control_id}'
        os.makedirs(work_dir)
        stale_state = f'{work_dir}/my-bucket.tfstate'
        with open(stale_state, 'w') as f:
            f.write('{"resources": ["stale"]}')

        s3 = MagicMock()
        with patch.object(handler, 's3', s3), \
             patch.object(handler, 'TF_CACHE', env['tf_cache']), \
             patch.object(handler, 'TF_LOCK', env['tf_lock']):
            returned = setup_remediation(control_id, f'controls/{control_id}/remediation.tf')

        assert returned == work_dir
        assert not os.path.exists(stale_state), \
            'stale tfstate must not survive into a new invocation'

    def test_workdir_rebuilt_with_cache_links(self, env):
        control_id = env['control_id']
        s3 = MagicMock()
        with patch.object(handler, 's3', s3), \
             patch.object(handler, 'TF_CACHE', env['tf_cache']), \
             patch.object(handler, 'TF_LOCK', env['tf_lock']):
            work_dir = setup_remediation(control_id, f'controls/{control_id}/remediation.tf')

        assert os.path.islink(f'{work_dir}/.terraform')
        assert os.readlink(f'{work_dir}/.terraform') == env['tf_cache']
        assert os.path.exists(f'{work_dir}/.terraform.lock.hcl')
        s3.download_file.assert_called_once_with(
            handler.REMEDIATION_BUCKET,
            f'controls/{control_id}/remediation.tf',
            f'{work_dir}/remediation.tf',
        )

    def test_repeated_calls_are_idempotent(self, env):
        control_id = env['control_id']
        s3 = MagicMock()
        with patch.object(handler, 's3', s3), \
             patch.object(handler, 'TF_CACHE', env['tf_cache']), \
             patch.object(handler, 'TF_LOCK', env['tf_lock']):
            setup_remediation(control_id, 'key')
            work_dir = setup_remediation(control_id, 'key')
        assert os.path.islink(f'{work_dir}/.terraform')
        assert s3.download_file.call_count == 2


class TestSessionPolicy:
    """The session policy is the authority for one remediation: these actions,
    this resource. IAM intersects it with the role, so it can only narrow."""

    def test_scopes_to_the_classified_resource(self):
        policy = handler.session_policy(
            ['s3:PutBucketVersioning'], 'arn:aws:s3:::my-test-bucket', 'resource')
        statement = policy['Statement'][0]
        assert statement['Action'] == ['s3:PutBucketVersioning']
        assert statement['Resource'] == ['arn:aws:s3:::my-test-bucket',
                                         'arn:aws:s3:::my-test-bucket/*']

    def test_account_level_control_has_no_arn_to_name(self):
        policy = handler.session_policy(
            ['ec2:EnableEbsEncryptionByDefault'], 'arn:aws:us-east-1:123456789012', 'account')
        assert policy['Statement'][0]['Resource'] == ['*']

    def test_action_outside_the_allowlist_is_refused(self):
        """A control is data in a table; the allowlist is code."""
        with pytest.raises(ValueError, match='allowlist'):
            handler.session_policy(['s3:DeleteBucket'], 'arn:aws:s3:::b', 'resource')

    def test_no_actions_is_refused(self):
        with pytest.raises(ValueError, match='no remediation_actions'):
            handler.session_policy([], 'arn:aws:s3:::b', 'resource')

    def test_handler_assumes_the_role_with_the_violation_as_session_name(self, scoped_session):
        with patch.object(handler, 'ensure_provider_cache'), \
             patch.object(handler, 'setup_remediation', return_value='/tmp/x'), \
             patch.object(handler.subprocess, 'run', return_value=completed(0, 'ok')), \
             patch('builtins.open', new_callable=MagicMock):
            result = lambda_handler(base_event(), None)
        assert result['remediation_status'] == 'success'
        call = scoped_session.call_args.kwargs
        assert call['RoleSessionName'] == 'vio-123'
        policy = json.loads(call['Policy'])
        assert policy['Statement'][0]['Resource'] == ['arn:aws:s3:::my-test-bucket',
                                                      'arn:aws:s3:::my-test-bucket/*']

    def test_tofu_runs_with_the_scoped_credentials(self):
        with patch.object(handler, 'ensure_provider_cache'), \
             patch.object(handler, 'setup_remediation', return_value='/tmp/x'), \
             patch.object(handler.subprocess, 'run', return_value=completed(0, 'ok')) as run, \
             patch('builtins.open', new_callable=MagicMock):
            lambda_handler(base_event(), None)
        env = run.call_args.kwargs['env']
        assert env['AWS_ACCESS_KEY_ID'] == 'AKIA-SCOPED'
        assert env['AWS_SESSION_TOKEN'] == 'token'

    def test_a_control_that_asks_for_too_much_never_runs_tofu(self):
        event = base_event(remediation_actions=['s3:PutBucketAcl'])
        with patch.object(handler, 'ensure_provider_cache'), \
             patch.object(handler, 'setup_remediation', return_value='/tmp/x'), \
             patch.object(handler.subprocess, 'run') as run:
            result = lambda_handler(event, None)
        assert result['remediation_status'] == 'failed'
        assert 'allowlist' in result['error']
        run.assert_not_called()
