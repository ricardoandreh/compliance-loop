"""
Analyze Lambda Handler (Dynamic Multi-Control, Single Invocation)

Evaluates ALL applicable controls for a resource in one invocation.
Returns a list of classification results — one per control.

This Lambda is a pure decision function — no side-effects beyond reads.
Persistence is handled by the Step Function.
"""

import boto3
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

controls_table = dynamodb.Table(os.environ.get('CONTROLS_TABLE', 'controls'))
exceptions_table = dynamodb.Table(os.environ.get('EXCEPTIONS_TABLE', 'exceptions'))
POLICY_BUCKET = os.environ.get('POLICY_BUCKET', '')


# --- Resource Config Fetchers (cached per invocation) ---

_config_cache: Dict[str, Any] = {}

def get_bucket_public_access_config(bucket_name: str) -> Dict[str, Any]:
    key = f'public_access:{bucket_name}'
    if key in _config_cache:
        return _config_cache[key]
    try:
        resp = s3_client.get_public_access_block(Bucket=bucket_name)
        cfg = resp['PublicAccessBlockConfiguration']
        result = {
            'BlockPublicAcls': cfg.get('BlockPublicAcls', False),
            'IgnorePublicAcls': cfg.get('IgnorePublicAcls', False),
            'BlockPublicPolicy': cfg.get('BlockPublicPolicy', False),
            'RestrictPublicBuckets': cfg.get('RestrictPublicBuckets', False)
        }
    except Exception as e:
        if 'NoSuchPublicAccessBlockConfiguration' in str(e):
            result = {'BlockPublicAcls': False, 'IgnorePublicAcls': False,
                      'BlockPublicPolicy': False, 'RestrictPublicBuckets': False}
        else:
            raise
    _config_cache[key] = result
    return result


def get_bucket_policy_statements(bucket_name: str) -> Dict[str, Any]:
    key = f'policy:{bucket_name}'
    if key in _config_cache:
        return _config_cache[key]
    try:
        resp = s3_client.get_bucket_policy(Bucket=bucket_name)
        policy = json.loads(resp['Policy'])
        result = {'policy_statements': policy.get('Statement', [])}
    except Exception as e:
        if 'NoSuchBucketPolicy' in str(e):
            result = {'policy_statements': []}
        else:
            raise
    _config_cache[key] = result
    return result


RESOURCE_FETCHERS = {
    's3_public_access': get_bucket_public_access_config,
    's3_ssl_only': get_bucket_policy_statements,
}


# --- OPA Evaluation ---

def evaluate_policy_with_opa(rego_package: str, resource_config: Dict[str, Any], rego_s3_key: str = None) -> bool:
    opa_input = {"resource": resource_config}
    policy_path = f'/tmp/policies/{rego_package}.rego'
    opa_bin = os.path.join(os.path.dirname(__file__), 'opa')
    input_path = '/tmp/opa_input.json'

    # Download rego from S3 if not cached
    if not os.path.exists(policy_path) and rego_s3_key and POLICY_BUCKET:
        os.makedirs('/tmp/policies', exist_ok=True)
        s3_client.download_file(POLICY_BUCKET, rego_s3_key, policy_path)

    # Fallback to bundled rego
    if not os.path.exists(policy_path):
        policy_path = os.path.join(os.path.dirname(__file__), f'{rego_package}.rego')

    with open(input_path, 'w') as f:
        json.dump(opa_input, f)
    try:
        result = subprocess.run(
            [opa_bin, 'eval', '--data', policy_path, '--input', input_path,
             '--format', 'json', f'data.{rego_package}.allow'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            print(f"OPA error ({rego_package}): {result.stderr}")
            return False
        output = json.loads(result.stdout)
        return bool(output.get('result', [{}])[0].get('expressions', [{}])[0].get('value', False))
    except Exception as e:
        print(f"OPA exception ({rego_package}): {e}")
        return False


# --- Exception Query ---

def query_exception(control_id: str, resource_arn: str) -> Optional[Dict[str, Any]]:
    try:
        resp = exceptions_table.query(
            IndexName='control_id-index',
            KeyConditionExpression='control_id = :cid',
            FilterExpression='resource_arn = :arn AND valid_until > :now',
            ExpressionAttributeValues={
                ':cid': control_id, ':arn': resource_arn, ':now': int(time.time())
            },
            Limit=1
        )
        items = resp.get('Items', [])
        return items[0] if items else None
    except Exception as e:
        print(f"DynamoDB query error: {e}")
        return None


# --- Classification ---

def classify_state(is_compliant: bool, exception: Optional[Dict[str, Any]]) -> str:
    if is_compliant:
        return "S_nominal"
    return "S_exception" if exception else "S_violation"


def extract_bucket_name(resource_arn: str) -> str:
    if not resource_arn.startswith('arn:aws:s3:::'):
        raise ValueError(f"Invalid S3 ARN: {resource_arn}")
    name = resource_arn.replace('arn:aws:s3:::', '')
    if not name:
        raise ValueError("Empty bucket name")
    return name


# --- Handler ---

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    print(f"Received event: {json.dumps(event)}")

    resource_arn = event['resource_arn']
    control_ids = event['control_ids']
    account_id = event['account_id']
    region = event['region']
    bucket_name = extract_bucket_name(resource_arn)
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    _config_cache.clear()
    results = []

    for control_id in control_ids:
        try:
            control = controls_table.get_item(Key={'control_id': control_id}).get('Item')
            if not control:
                raise ValueError(f"Control not found: {control_id}")

            rego_package = control['rego_policy_package']
            policy_version = f"{rego_package}:{control.get('policy_version', 'v1.0')}"

            fetcher = RESOURCE_FETCHERS.get(rego_package)
            if not fetcher:
                raise ValueError(f"No fetcher for: {rego_package}")

            resource_config = fetcher(bucket_name)
            is_compliant = evaluate_policy_with_opa(rego_package, resource_config, control.get('rego_s3_key'))
            exception = query_exception(control_id, resource_arn)
            state = classify_state(is_compliant, exception)

            results.append({
                'control_id': control_id,
                'state': state,
                'violation_id': str(uuid.uuid4()),
                'timestamp': now,
                'policy_version': policy_version,
                'exception_id': exception.get('exception_id') if exception else None,
                'approved_by': exception.get('approved_by') if exception else None,
                'remediation_s3_key': control.get('remediation_s3_key'),
            })
            print(f"  {control_id}: {state}")

        except Exception as e:
            print(f"  {control_id}: ERROR - {e}")
            results.append({
                'control_id': control_id,
                'state': 'S_error',
                'violation_id': str(uuid.uuid4()),
                'timestamp': now,
                'policy_version': 'unknown',
                'exception_id': None,
                'approved_by': None,
                'error': str(e),
            })

    return {
        'resource_arn': resource_arn,
        'account_id': account_id,
        'region': region,
        'results': results
    }
