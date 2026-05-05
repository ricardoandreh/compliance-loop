import subprocess
import json
import os
import shutil
import tarfile
import boto3
from typing import Dict, Any

s3 = boto3.client('s3')
REMEDIATION_BUCKET = os.environ['REMEDIATION_BUCKET']
PROVIDER_CACHE_KEY = os.environ.get('PROVIDER_CACHE_KEY', 'provider-cache/tf-dot-terraform.tar.gz')
TF_CACHE = '/tmp/tf-dot-terraform'
TF_LOCK = '/tmp/tf-lock.hcl'


def ensure_provider_cache():
    """Download and extract pre-initialized .terraform from S3 on cold start."""
    if os.path.exists(TF_CACHE):
        return
    archive = '/tmp/tf-cache.tar.gz'
    s3.download_file(REMEDIATION_BUCKET, PROVIDER_CACHE_KEY, archive)
    with tarfile.open(archive, 'r:gz') as tar:
        tar.extractall(path='/tmp/')
    os.remove(archive)


def extract_bucket_name(resource_arn: str) -> str:
    return resource_arn.split(':::')[1]


def setup_remediation(control_id: str, s3_key: str) -> str:
    """Download HCL from S3, link pre-cached provider, return working dir."""
    work_dir = f'/tmp/{control_id}'
    tf_path = f'{work_dir}/remediation.tf'

    if not os.path.exists(tf_path):
        os.makedirs(work_dir, exist_ok=True)
        s3.download_file(REMEDIATION_BUCKET, s3_key, tf_path)
        os.symlink(TF_CACHE, f'{work_dir}/.terraform')
        shutil.copy(TF_LOCK, f'{work_dir}/.terraform.lock.hcl')

    return work_dir


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    resource_arn = event['resource_arn']
    region = event['region']
    control_id = event['control_id']
    s3_key = event['remediation_s3_key']
    bucket_name = extract_bucket_name(resource_arn)

    try:
        ensure_provider_cache()
        work_dir = setup_remediation(control_id, s3_key)
    except Exception as e:
        return {"remediation_status": "failed", "error": f"Setup failed: {e}"}

    state_path = f'/tmp/{control_id}/{bucket_name}.tfstate'

    tfvars = {"bucket_name": bucket_name, "region": region}
    if control_id == 'ctrl-s3-ssl-only':
        try:
            resp = s3.get_bucket_policy(Bucket=bucket_name)
            tfvars['existing_policy'] = resp['Policy']
        except Exception:
            pass

    with open(f'{work_dir}/terraform.tfvars.json', 'w') as f:
        json.dump(tfvars, f)

    try:
        result = subprocess.run(
            ['/opt/bin/tofu', 'apply', '-refresh=false', '-auto-approve', '-no-color',
             f'-var-file={work_dir}/terraform.tfvars.json', f'-state={state_path}'],
            cwd=work_dir, capture_output=True, text=True, timeout=80
        )
        if result.returncode == 0:
            return {"remediation_status": "success", "output": result.stdout}
        return {"remediation_status": "failed", "error": result.stderr}

    except subprocess.TimeoutExpired as e:
        return {"remediation_status": "failed", "error": f"OpenTofu timed out: {e}"}
    except Exception as e:
        return {"remediation_status": "failed", "error": f"Unexpected error: {e}"}
