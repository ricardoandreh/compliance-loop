import subprocess
import json
import os
import shutil
from typing import Dict, Any


def extract_bucket_name(resource_arn: str) -> str:
    """
    Extract bucket name from S3 ARN.
    
    Args:
        resource_arn: S3 bucket ARN in format arn:aws:s3:::bucket-name
        
    Returns:
        Bucket name
    """
    # ARN format: arn:aws:s3:::bucket-name
    return resource_arn.split(':::')[1]


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Execute Lambda handler for remediating S3 bucket public access configuration.
    
    Uses OpenTofu to apply compliant configuration to S3 buckets in violation.
    
    Args:
        event: Event from Step Functions containing resource_arn and region
        context: Lambda context object
        
    Returns:
        Dict with remediation_status ("success" or "failed") and output/error
    """
    # 1. Extract resource metadata
    resource_arn = event['resource_arn']
    region = event['region']
    bucket_name = extract_bucket_name(resource_arn)
    
    # 2. Warm container cleanup - remove existing /tmp/remediation/
    if os.path.exists('/tmp/remediation'):
        shutil.rmtree('/tmp/remediation')
    
    # 3. Create /tmp/remediation/ directory
    os.makedirs('/tmp/remediation', exist_ok=True)
    
    # 4. Copy remediation.tf to /tmp/remediation/
    shutil.copy('/opt/remediation/remediation.tf', '/tmp/remediation/')
    
    # 5. Copy .terraform/ directory to /tmp/remediation/.terraform/
    shutil.copytree('/opt/remediation/.terraform', '/tmp/remediation/.terraform')
    
    # 6. Write terraform.tfvars.json with bucket_name and region
    tfvars = {
        "bucket_name": bucket_name,
        "region": region
    }
    with open('/tmp/terraform.tfvars.json', 'w') as f:
        json.dump(tfvars, f)
    
    # 7. Execute tofu apply
    try:
        result = subprocess.run(
            [
                'tofu', 'apply',
                '-refresh=false',
                '-auto-approve',
                '-var-file=/tmp/terraform.tfvars.json',
                '-state=/tmp/remediation.tfstate',
                '-plugin-dir=/opt/providers'
            ],
            cwd='/tmp/remediation',
            capture_output=True,
            text=True,
            timeout=40
        )
        
        # 8. Return success or failure based on returncode
        if result.returncode == 0:
            return {
                "remediation_status": "success",
                "output": result.stdout
            }
        else:
            return {
                "remediation_status": "failed",
                "error": result.stderr
            }
            
    except subprocess.TimeoutExpired as e:
        return {
            "remediation_status": "failed",
            "error": f"OpenTofu apply timed out after 40 seconds: {str(e)}"
        }
    except Exception as e:
        return {
            "remediation_status": "failed",
            "error": f"Unexpected error during remediation: {str(e)}"
        }
