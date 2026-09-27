import subprocess
import json
import os
import shutil
import tarfile
import time
import boto3
from typing import Dict, Any

s3 = boto3.client('s3')
sts = boto3.client('sts')
REMEDIATION_BUCKET = os.environ['REMEDIATION_BUCKET']
REMEDIATION_ROLE_ARN = os.environ['REMEDIATION_ROLE_ARN']
PROVIDER_CACHE_KEY = os.environ.get('PROVIDER_CACHE_KEY', 'provider-cache/tf-dot-terraform.tar.zst')
TF_CACHE = '/tmp/tf-dot-terraform'
TF_LOCK = '/tmp/tf-lock.hcl'


def ensure_provider_cache():
    """The pre-initialized .terraform/ (AWS provider + lock), unpacked in /tmp.

    zstd, not gzip: measured in the Lambda, unpacking the 780 MB provider went
    from 5.1 s (gzip, in the handler) to about 3.5 s, and the archive is 21%
    smaller (scripts/perf_experiment.py, results/perf/).
    """
    if os.path.exists(TF_CACHE):
        return
    from compression import zstd
    archive = '/tmp/tf-cache.tar.zst'
    s3.download_file(REMEDIATION_BUCKET, PROVIDER_CACHE_KEY, archive)
    with zstd.open(archive) as stream, tarfile.open(fileobj=stream, mode='r|') as tar:
        tar.extractall(path='/tmp/', filter='data')
    os.remove(archive)


# The init phase runs with full CPU whatever the memory setting, so the cold
# start's largest cost - fetching and unpacking the provider - is paid there
# (measured: 12.6 s -> 10.9 s cold at the same memory). Only inside Lambda: an
# import in the tests must not reach S3. A failure here is left to the handler,
# which fetches again and reports it as a failed remediation.
_cold = True
if os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
    try:
        ensure_provider_cache()
    except Exception as e:
        print(f"provider cache at init failed, deferring to the handler: {e}")


# The only write actions any control may ask for. A control is data in a
# DynamoDB table; this list is not, so a tampered control cannot reach an API
# outside it — and the session policy below can only narrow the role further.
# It comes from infra/modules/execute (local.remediation_actions), the same list
# the remediation role grants: one source, so adding an API is one Terraform
# change and no code change. Required: a deploy that forgets it refuses every
# remediation instead of allowing any.
ALLOWED_ACTIONS = frozenset(json.loads(os.environ['ALLOWED_ACTIONS']))


def session_policy(actions, resource_arn, scope):
    """Least privilege for one remediation: these actions, on this resource.

    The resource comes from the evaluation that classified it, never from the
    control definition — a control chooses which APIs its remediation needs, and
    nothing else. IAM intersects this with the role's own policy, so the session
    can only ever be narrower than the role.
    """
    unknown = sorted(set(actions) - ALLOWED_ACTIONS)
    if unknown:
        raise ValueError(f"control asks for actions outside the allowlist: {unknown}")
    if not actions:
        raise ValueError("control declares no remediation_actions")

    if scope == 'account':
        # Account-wide settings (EBS encryption by default) have no ARN to name.
        resources = ['*']
    elif resource_arn.startswith('arn:aws:s3:::'):
        # Bucket APIs act on the bucket; the object form keeps a future
        # object-level remediation inside the same scope.
        resources = [resource_arn, f'{resource_arn}/*']
    else:
        resources = [resource_arn]

    return {'Version': '2012-10-17',
            'Statement': [{'Effect': 'Allow', 'Action': sorted(actions), 'Resource': resources}]}


def remediation_credentials(actions, resource_arn, scope, violation_id):
    """Assume the remediation role for this violation only.

    RoleSessionName carries the violation_id, so every API call the remediation
    makes is attributable in CloudTrail to the violation that motivated it — the
    audit chain of H2, confirmed from the provider's side and not only from the
    system's own log.
    """
    resp = sts.assume_role(
        RoleArn=REMEDIATION_ROLE_ARN,
        RoleSessionName=violation_id[:64],
        Policy=json.dumps(session_policy(actions, resource_arn, scope)),
        DurationSeconds=900,
    )
    c = resp['Credentials']
    return {'AWS_ACCESS_KEY_ID': c['AccessKeyId'],
            'AWS_SECRET_ACCESS_KEY': c['SecretAccessKey'],
            'AWS_SESSION_TOKEN': c['SessionToken']}


def setup_remediation(control_id: str, s3_key: str) -> str:
    """Prepara um diretório de trabalho limpo para o tofu apply.

    O work_dir é recriado a cada invocação. Isso elimina qualquer tfstate
    de uma invocação warm anterior — combinado com -refresh=false, um state
    obsoleto faria o OpenTofu reportar 'No changes' para um recurso
    re-violado, pulando a remediação silenciosamente.
    """
    work_dir = f'/tmp/{control_id}'
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir, exist_ok=True)
    s3.download_file(REMEDIATION_BUCKET, s3_key, f'{work_dir}/remediation.tf')
    os.symlink(TF_CACHE, f'{work_dir}/.terraform')
    shutil.copy(TF_LOCK, f'{work_dir}/.terraform.lock.hcl')
    return work_dir


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Remediate, and log how long each phase took (one JSON line per call)."""
    global _cold
    timings = {}
    start = time.perf_counter()
    try:
        return _remediate(event, timings)
    finally:
        timings['total'] = time.perf_counter() - start
        print(json.dumps({"timings": {k: round(v, 4) for k, v in timings.items()},
                          "control_id": event.get('control_id'), "cold": _cold}))
        _cold = False


class _phase:
    """Adds the block's wall time to timings[name]."""
    def __init__(self, timings, name):
        self.timings, self.name = timings, name

    def __enter__(self):
        self.t = time.perf_counter()

    def __exit__(self, *exc):
        self.timings[self.name] = self.timings.get(self.name, 0) + time.perf_counter() - self.t


def scoped_s3(creds, region):
    """An S3 client that signs with the violation's session credentials.

    The credentials carry the session policy; the client is only the signer.
    Built from the default session, it reuses that session's parsed service
    models instead of loading them again, as a new boto3.Session would.
    """
    return boto3.client('s3', region_name=region,
                        aws_access_key_id=creds['AWS_ACCESS_KEY_ID'],
                        aws_secret_access_key=creds['AWS_SECRET_ACCESS_KEY'],
                        aws_session_token=creds['AWS_SESSION_TOKEN'])


def _remediate(event: Dict[str, Any], timings: Dict[str, float]) -> Dict[str, Any]:
    resource_arn = event['resource_arn']
    region = event['region']
    control_id = event['control_id']
    s3_key = event['remediation_s3_key']
    resource_id = event.get('resource_id', '')
    bucket_name = resource_arn.split(':::')[1] if ':::' in resource_arn else resource_id

    try:
        with _phase(timings, 'assume_role'):
            creds = remediation_credentials(
                event.get('remediation_actions', []), resource_arn,
                event.get('remediation_scope', 'resource'), event.get('violation_id', 'remediation'))
    except Exception as e:
        return {"remediation_status": "failed", "error": f"Could not scope the remediation session: {e}"}

    # Every AWS call from here on uses the scoped credentials, including the
    # policy read below: the Lambda's own role can no longer touch the resource.

    try:
        with _phase(timings, 'provider_cache'):
            ensure_provider_cache()
        with _phase(timings, 'setup'):
            work_dir = setup_remediation(control_id, s3_key)
    except Exception as e:
        return {"remediation_status": "failed", "error": f"Setup failed: {e}"}

    state_path = f'/tmp/{control_id}/{bucket_name}.tfstate'
    tfvars = {"bucket_name": bucket_name, "region": region, "resource_id": resource_id}

    # Edge case: ssl-only merges the SSL statement into the existing policy.
    # Only NoSuchBucketPolicy may be treated as "no policy" — any other error
    # (AccessDenied, throttling, timeout) MUST abort: proceeding without the
    # existing policy would overwrite it with just the SSL statement,
    # silently destroying the customer's statements.
    if control_id == 'ctrl-s3-ssl-only':
        try:
            with _phase(timings, 'policy_read'):
                # Built only here, the one control that reads before writing.
                resp = scoped_s3(creds, region).get_bucket_policy(Bucket=bucket_name)
            tfvars['existing_policy'] = resp['Policy']
        except Exception as e:
            code = getattr(e, 'response', {}).get('Error', {}).get('Code', '')
            if code != 'NoSuchBucketPolicy':
                return {"remediation_status": "failed",
                        "error": f"Cannot read existing bucket policy ({code or e}); "
                                 f"refusing to remediate without it"}

    with open(f'{work_dir}/terraform.tfvars.json', 'w') as f:
        json.dump(tfvars, f)

    try:
        with _phase(timings, 'tofu'):
            result = subprocess.run(
                ['/opt/bin/tofu', 'apply', '-refresh=false', '-auto-approve', '-no-color',
                 f'-var-file={work_dir}/terraform.tfvars.json', f'-state={state_path}'],
                cwd=work_dir, capture_output=True, text=True, timeout=65,
                env={**os.environ, **creds}
            )
        if result.returncode == 0:
            return {"remediation_status": "success", "output": result.stdout}
        return {"remediation_status": "failed", "error": result.stderr}

    except subprocess.TimeoutExpired as e:
        return {"remediation_status": "failed", "error": f"OpenTofu timed out: {e}"}
    except Exception as e:
        return {"remediation_status": "failed", "error": f"Unexpected error: {e}"}
