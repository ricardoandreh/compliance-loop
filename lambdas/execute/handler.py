import subprocess
import json
import os
from typing import Dict, Any


def extract_bucket_name(resource_arn: str) -> str:
    return resource_arn.split(':::')[1]


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    resource_arn = event['resource_arn']
    region = event['region']
    bucket_name = extract_bucket_name(resource_arn)

    state_dir = f'/tmp/{bucket_name}'
    os.makedirs(state_dir, exist_ok=True)
    state_path = f'{state_dir}/terraform.tfstate'

    # Write tfvars
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
            cwd='/opt/remediation',
            capture_output=True,
            text=True,
            timeout=80
        )

        if result.returncode == 0:
            return {"remediation_status": "success", "output": result.stdout}
        else:
            return {"remediation_status": "failed", "error": result.stderr}

    except subprocess.TimeoutExpired as e:
        return {"remediation_status": "failed", "error": f"OpenTofu apply timed out: {str(e)}"}
    except Exception as e:
        return {"remediation_status": "failed", "error": f"Unexpected error: {str(e)}"}
