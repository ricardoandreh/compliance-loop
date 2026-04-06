"""
Analyze Lambda Handler

Classifies S3 bucket compliance state by:
1. Retrieving current resource configuration from AWS
2. Evaluating against Rego policy using OPA
3. Checking for valid exceptions in DynamoDB
4. Logging evaluation to violations_log
5. Returning classification state
"""

import boto3
import json
import os
import subprocess
import time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional


# Initialize AWS clients
s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# DynamoDB table references
exceptions_table = dynamodb.Table(os.environ.get('EXCEPTIONS_TABLE', 'exceptions'))
violations_log_table = dynamodb.Table(os.environ.get('VIOLATIONS_LOG_TABLE', 'violations_log'))

# Policy version for audit trail
POLICY_VERSION = "s3_public_access:v1.0"


def extract_bucket_name(resource_arn: str) -> str:
    """
    Extract bucket name from S3 ARN.
    
    Args:
        resource_arn: ARN in format arn:aws:s3:::bucket-name
        
    Returns:
        Bucket name
    """
    if not resource_arn.startswith('arn:aws:s3:::'):
        raise ValueError(f"Invalid S3 ARN format: {resource_arn}")
    
    return resource_arn.replace('arn:aws:s3:::', '')


def get_bucket_public_access_config(bucket_name: str) -> Dict[str, bool]:
    """
    Retrieve current public access block configuration from S3.
    
    Args:
        bucket_name: Name of the S3 bucket
        
    Returns:
        Dictionary with BlockPublicAcls, IgnorePublicAcls, 
        BlockPublicPolicy, RestrictPublicBuckets
        
    Raises:
        ClientError: If bucket doesn't exist or access denied
    """
    try:
        response = s3_client.get_public_access_block(Bucket=bucket_name)
        config = response['PublicAccessBlockConfiguration']
        
        return {
            'BlockPublicAcls': config.get('BlockPublicAcls', False),
            'IgnorePublicAcls': config.get('IgnorePublicAcls', False),
            'BlockPublicPolicy': config.get('BlockPublicPolicy', False),
            'RestrictPublicBuckets': config.get('RestrictPublicBuckets', False)
        }
    except s3_client.exceptions.NoSuchPublicAccessBlockConfiguration:
        # No configuration means all settings are false
        return {
            'BlockPublicAcls': False,
            'IgnorePublicAcls': False,
            'BlockPublicPolicy': False,
            'RestrictPublicBuckets': False
        }


def evaluate_policy_with_opa(resource_config: Dict[str, bool]) -> bool:
    """
    Evaluate resource configuration against Rego policy using OPA.
    
    Args:
        resource_config: S3 public access block configuration
        
    Returns:
        True if compliant (policy allows), False if non-compliant
    """
    # Prepare OPA input
    opa_input = {
        "resource": resource_config
    }
    
    # Path to Rego policy file (bundled in deployment package)
    policy_path = os.path.join(os.path.dirname(__file__), 's3_public_access.rego')
    
    try:
        # Execute OPA eval command
        result = subprocess.run(
            [
                'opa', 'eval',
                '--data', policy_path,
                '--input', '-',
                '--format', 'json',
                'data.s3_public_access.allow'
            ],
            input=json.dumps(opa_input),
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode != 0:
            print(f"OPA evaluation error: {result.stderr}")
            # Fail-secure: treat evaluation error as violation
            return False
        
        # Parse OPA output
        output = json.loads(result.stdout)
        allow = output.get('result', [{}])[0].get('expressions', [{}])[0].get('value', False)
        
        return bool(allow)
        
    except subprocess.TimeoutExpired:
        print("OPA evaluation timeout")
        return False
    except Exception as e:
        print(f"OPA evaluation exception: {str(e)}")
        # Fail-secure: treat exceptions as violations
        return False


def query_exception(control_id: str, resource_arn: str) -> Optional[Dict[str, Any]]:
    """
    Query DynamoDB for valid exception matching control_id and resource_arn.
    
    Args:
        control_id: Control identifier
        resource_arn: Resource ARN to match
        
    Returns:
        Exception item if valid exception exists, None otherwise
    """
    current_timestamp = int(time.time())
    
    try:
        # Query using GSI control_id-index
        response = exceptions_table.query(
            IndexName='control_id-index',
            KeyConditionExpression='control_id = :cid',
            FilterExpression='resource_arn = :arn AND valid_until > :now',
            ExpressionAttributeValues={
                ':cid': control_id,
                ':arn': resource_arn,
                ':now': current_timestamp
            },
            Limit=1  # We only need one valid exception
        )
        
        items = response.get('Items', [])
        return items[0] if items else None
        
    except Exception as e:
        print(f"DynamoDB query error: {str(e)}")
        # Fail-secure: proceed without exception on query errors
        return None


def classify_state(is_compliant: bool, exception: Optional[Dict[str, Any]]) -> str:
    """
    Classify resource compliance state based on policy evaluation and exceptions.
    
    Args:
        is_compliant: Result of policy evaluation
        exception: Valid exception if exists
        
    Returns:
        State classification: S_nominal, S_exception, or S_violation
    """
    if is_compliant:
        return "S_nominal"
    elif exception is not None:
        return "S_exception"
    else:
        return "S_violation"


def log_violation(
    violation_id: str,
    resource_arn: str,
    control_id: str,
    state: str,
    account_id: str,
    region: str,
    exception: Optional[Dict[str, Any]] = None
) -> None:
    """
    Write evaluation entry to violations_log table.
    
    Args:
        violation_id: Unique violation identifier (UUID)
        resource_arn: Resource ARN
        control_id: Control identifier
        state: Classification state
        account_id: AWS account ID
        region: AWS region
        exception: Exception item if state is S_exception
    """
    timestamp = datetime.utcnow().isoformat() + 'Z'
    
    item = {
        'violation_id': violation_id,
        'resource_arn': resource_arn,
        'control_id': control_id,
        'state': state,
        'timestamp': timestamp,
        'policy_version': POLICY_VERSION,
        'account_id': account_id,
        'region': region
    }
    
    # Add exception details if state is S_exception
    if state == "S_exception" and exception:
        item['exception_id'] = exception.get('exception_id')
        item['approved_by'] = exception.get('approved_by')
    
    try:
        violations_log_table.put_item(Item=item)
    except Exception as e:
        print(f"Failed to write to violations_log: {str(e)}")
        # Don't fail the Lambda if logging fails
        raise


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for Analyze component.
    
    Args:
        event: Input event with resource_arn, control_id, account_id, region, event_time
        context: Lambda context object
        
    Returns:
        Dictionary with state, violation_id, exception_id, approved_by
    """
    print(f"Received event: {json.dumps(event)}")
    
    # 1. Extract resource metadata
    resource_arn = event['resource_arn']
    control_id = event['control_id']
    account_id = event['account_id']
    region = event['region']
    
    bucket_name = extract_bucket_name(resource_arn)
    print(f"Analyzing bucket: {bucket_name}")
    
    # 2. Retrieve current resource configuration
    resource_config = get_bucket_public_access_config(bucket_name)
    print(f"Resource configuration: {json.dumps(resource_config)}")
    
    # 3. Evaluate with OPA
    is_compliant = evaluate_policy_with_opa(resource_config)
    print(f"Policy evaluation result: {'compliant' if is_compliant else 'non-compliant'}")
    
    # 4. Check for valid exception
    exception = query_exception(control_id, resource_arn)
    if exception:
        print(f"Found valid exception: {exception['exception_id']}")
    
    # 5. Classify state
    state = classify_state(is_compliant, exception)
    print(f"Classification: {state}")
    
    # 6. Generate violation_id
    violation_id = str(uuid.uuid4())
    
    # 7. Log to violations_log
    log_violation(
        violation_id=violation_id,
        resource_arn=resource_arn,
        control_id=control_id,
        state=state,
        account_id=account_id,
        region=region,
        exception=exception
    )
    
    # 8. Return classification result
    result = {
        'state': state,
        'violation_id': violation_id,
        'exception_id': exception.get('exception_id') if exception else None,
        'approved_by': exception.get('approved_by') if exception else None,
        'resource_arn': resource_arn,
        'control_id': control_id,
        'account_id': account_id,
        'region': region
    }
    
    print(f"Returning result: {json.dumps(result)}")
    return result
