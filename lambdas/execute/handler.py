import subprocess
import json
import os
from typing import Dict, Any


def extract_bucket_name(resource_arn: str) -> str:
    return resource_arn.split(':::')[1]


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    resource_arn = event['resource_arn']
    region = event['region']
    control_id = event['control_id']
    bucket_name = extract_bucket_name(resource_arn)

    remediation_dir = f'/opt/remediation/{control_id}'
    if not os.path.isdir(remediation_dir):
        return {"remediation_status": "failed", "error": f"No remediation for: {control_id}"}

    # State isolated by control_id/resource_id
    state_dir = f'/tmp/{control_id}/{bucket_name}'
    os.makedirs(state_dir, exist_ok=True)
    state_path = f'{state_dir}/terraform.tfstate'

    with open('/tmp/terraform.tfvars.json', 'w') as f:
        json.dump({"bucket_name": bucket_name, "region": region}, f)

    try:
        result = subprocess.run(
            [
                'tofu', 'apply',
                '-refresh=false',
                '-auto-approve',
                '-no-color',
                '-var-file=/tmp/terraform.tfvars.json',
                f'-state={state_path}'
            ],
            cwd=remediation_dir,
            capture_output=True,
            text=True,
            timeout=80
        )

        if result.returncode == 0:
            return {"remediation_status": "success", "output": result.stdout}
        return {"remediation_status": "failed", "error": result.stderr}

    except subprocess.TimeoutExpired as e:
        return {"remediation_status": "failed", "error": f"OpenTofu timed out: {e}"}
    except Exception as e:
        return {"remediation_status": "failed", "error": f"Unexpected error: {e}"}
