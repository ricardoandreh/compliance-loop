"""
Analyze Lambda Handler (Dynamic Multi-Control, Single Invocation)

Evaluates ALL applicable controls for a resource in one invocation.
Returns a list of classification results — one per control.

Resource configuration is fetched dynamically based on `fetch_config`
stored in the controls DynamoDB table — no hardcoded fetchers.
"""

import boto3
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional

s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

controls_table = dynamodb.Table(os.environ.get('CONTROLS_TABLE', 'controls'))
exceptions_table = dynamodb.Table(os.environ.get('EXCEPTIONS_TABLE', 'exceptions'))
POLICY_BUCKET = os.environ.get('POLICY_BUCKET', '')

_boto_clients: Dict[str, Any] = {}
_config_cache: Dict[str, Any] = {}


# --- Generic Resource Fetcher ---

def get_boto_client(service: str):
    if service not in _boto_clients:
        _boto_clients[service] = boto3.client(service)
    return _boto_clients[service]


def fetch_resource_config(fetch_config: Dict[str, Any], resource_id: str) -> Dict[str, Any]:
    """Fetch resource configuration using boto3 call declared in fetch_config."""
    cache_key = f"{fetch_config['method']}:{resource_id}"
    if cache_key in _config_cache:
        return _config_cache[cache_key]

    client = get_boto_client(fetch_config['service'])
    method = getattr(client, fetch_config['method'])

    # Substitute {resource_id} in params
    params = json.loads(json.dumps(fetch_config.get('params', {})).replace('{resource_id}', resource_id))

    try:
        result = method(**params)
    except Exception as e:
        error_code = getattr(e, 'response', {}).get('Error', {}).get('Code', '')
        defaults = fetch_config.get('error_defaults', {})
        if error_code in defaults:
            result = defaults[error_code]
            _config_cache[cache_key] = result
            return result
        raise

    # Navigate result_path
    for key in fetch_config.get('result_path', '').split('.'):
        if key:
            result = result[key]

    _config_cache[cache_key] = result
    return result


# --- OPA Evaluation ---

def evaluate_policy_with_opa(rego_package: str, resource_config: Dict[str, Any], rego_s3_key: str = None) -> bool:
    opa_input = {"resource": resource_config}
    policy_path = f'/tmp/policies/{rego_package}.rego'
    input_path = '/tmp/opa_input.json'

    if not os.path.exists(policy_path) and rego_s3_key and POLICY_BUCKET:
        os.makedirs('/tmp/policies', exist_ok=True)
        s3_client.download_file(POLICY_BUCKET, rego_s3_key, policy_path)

    if not os.path.exists(policy_path):
        policy_path = os.path.join(os.path.dirname(__file__), f'{rego_package}.rego')

    with open(input_path, 'w') as f:
        json.dump(opa_input, f)
    try:
        result = subprocess.run(
            ['/opt/bin/opa', 'eval', '--data', policy_path, '--input', input_path,
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


def extract_resource_id(resource_arn: str) -> str:
    if resource_arn.startswith('arn:aws:s3:::'):
        name = resource_arn.replace('arn:aws:s3:::', '')
        if not name:
            raise ValueError("Empty bucket name")
        return name
    # Generic: last segment after : or /
    return resource_arn.split(':')[-1].split('/')[-1]


# --- Handler ---

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    print(f"Received event: {json.dumps(event)}")

    resource_arn = event['resource_arn']
    control_ids = event['control_ids']
    account_id = event['account_id']
    region = event['region']
    resource_id = extract_resource_id(resource_arn)
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

            fetch_config = control.get('fetch_config')
            if not fetch_config:
                raise ValueError(f"No fetch_config for: {control_id}")

            resource_config = fetch_resource_config(fetch_config, resource_id)
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
