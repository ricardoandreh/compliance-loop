import subprocess
import json
import os
import boto3
from typing import Dict, Any

s3 = boto3.client('s3')
REMEDIATION_BUCKET = os.environ['REMEDIATION_BUCKET']
PLUGIN_DIR = os.environ.get('TOFU_PLUGIN_DIR', '/opt/providers')


def extract_bucket_name(resource_arn: str) -> str:
    return resource_arn.split(':::')[1]


def setup_remediation(control_id: str, s3_key: str) -> str:
    """Download HCL from S3, link pre-init'd provider, return working dir."""
    work_dir = f'/tmp/{control_id}'
    tf_path = f'{work_dir}/remediation.tf'

    if not os.path.exists(tf_path):
        os.makedirs(work_dir, exist_ok=True)
        s3.download_file(REMEDIATION_BUCKET, s3_key, tf_path)
        os.symlink('/opt/tf-dot-terraform', f'{work_dir}/.terraform')
        subprocess.run(['cp', '/opt/tf-lock.hcl', f'{work_dir}/.terraform.lock.hcl'], check=True)

    return work_dir


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    resource_arn = event['resource_arn']
    region = event['region']
    control_id = event['control_id']
    s3_key = event['remediation_s3_key']
    bucket_name = extract_bucket_name(resource_arn)

    try:
        work_dir = setup_remediation(control_id, s3_key)
    except Exception as e:
        return {"remediation_status": "failed", "error": f"Setup failed: {e}"}

    state_path = f'/tmp/{control_id}/{bucket_name}.tfstate'

    with open(f'{work_dir}/terraform.tfvars.json', 'w') as f:
        json.dump({"bucket_name": bucket_name, "region": region}, f)

    try:
        result = subprocess.run(
            ['tofu', 'apply', '-refresh=false', '-auto-approve', '-no-color',
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
