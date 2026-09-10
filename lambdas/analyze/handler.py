"""
Analyze Lambda Handler (Dynamic Multi-Control, Single Invocation)

Receives pre-fetched control definitions from Step Function (QueryControls).
No DynamoDB reads for controls table — only exceptions table is queried.
"""

import boto3
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from boto3.dynamodb.types import TypeDeserializer

s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
exceptions_table = dynamodb.Table(os.environ.get('EXCEPTIONS_TABLE', 'exceptions'))
POLICY_BUCKET = os.environ.get('POLICY_BUCKET', '')

_deserializer = TypeDeserializer()
_boto_clients: Dict[str, Any] = {}
_config_cache: Dict[str, Any] = {}


# --- Canonical audit timestamp ---
#
# INVARIANT: every value written to violations_log.timestamp (the range key of
# resource_arn-timestamp-index) MUST come from this function. The audit GSI
# relies on ISO-8601 sorting lexicographically == chronologically, which only
# holds if ALL writers use an identical format: UTC, millisecond precision,
# zero-padded, with a literal 'Z' suffix (never '+00:00'). Mixing formats
# (microseconds, no padding, offset suffix) silently corrupts the per-resource
# timeline. This Lambda is the sole logical writer today (Step Functions copies
# analyze_result.timestamp verbatim into putItem); keep it that way and any new
# writer must reuse this canonical form.
def canonical_timestamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def deserialize_item(item: Dict) -> Dict:
    """Convert DynamoDB typed item to plain dict."""
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


# --- Generic Resource Fetcher ---

def get_boto_client(service: str):
    if service not in _boto_clients:
        _boto_clients[service] = boto3.client(service)
    return _boto_clients[service]


def _substitute_params(obj: Any, resource_id: str) -> Any:
    """Replace the {resource_id} placeholder structurally — string-replacing
    the serialized JSON would corrupt it for ids containing quotes/backslashes."""
    if isinstance(obj, str):
        return obj.replace('{resource_id}', resource_id)
    if isinstance(obj, dict):
        return {k: _substitute_params(v, resource_id) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_params(v, resource_id) for v in obj]
    return obj


def fetch_resource_config(fetch_config: Dict[str, Any], resource_id: str) -> Dict[str, Any]:
    cache_key = f"{fetch_config['method']}:{resource_id}"
    if cache_key in _config_cache:
        return _config_cache[cache_key]

    client = get_boto_client(fetch_config['service'])
    method = getattr(client, fetch_config['method'])
    params = _substitute_params(fetch_config.get('params', {}), resource_id)

    try:
        result = method(**params)
    except Exception as e:
        error_code = getattr(e, 'response', {}).get('Error', {}).get('Code', '')
        defaults = fetch_config.get('error_defaults', {})
        if error_code in defaults:
            _config_cache[cache_key] = defaults[error_code]
            return _config_cache[cache_key]
        raise

    for key in fetch_config.get('result_path', '').split('.'):
        if key:
            result = result[key]

    _config_cache[cache_key] = result
    return result


# --- OPA Evaluation ---

class PolicyEvaluationError(Exception):
    """Raised when OPA cannot evaluate the policy (vs. a policy that denies).

    Evaluation failure must NOT be conflated with non-compliance: classifying
    an unevaluated resource as S_violation triggers remediation based on no
    policy decision at all. The caller escalates (S_error) instead.
    """
    pass


def evaluate_policy_with_opa(rego_package: str, resource_config: Dict[str, Any],
                             rego_s3_key: str = None, policy_version: str = 'v1.0') -> bool:
    opa_input = {"resource": resource_config}
    # Version-keyed cache: warm containers must not keep evaluating a stale
    # .rego after the control's policy_version changes in DynamoDB — the log
    # would then claim a policy_version that was never actually executed.
    policy_path = f'/tmp/policies/{rego_package}@{policy_version}.rego'
    input_path = '/tmp/opa_input.json'

    if not os.path.exists(policy_path) and rego_s3_key and POLICY_BUCKET:
        os.makedirs('/tmp/policies', exist_ok=True)
        s3_client.download_file(POLICY_BUCKET, rego_s3_key, policy_path)

    try:
        # default=str: boto3 responses may carry datetime objects (RDS,
        # CloudTrail, ...); without it the dump raises before OPA even runs.
        with open(input_path, 'w') as f:
            json.dump(opa_input, f, default=str)

        result = subprocess.run(
            ['/opt/bin/opa', 'eval', '--data', policy_path, '--input', input_path,
             '--format', 'json', f'data.{rego_package}.allow'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            raise PolicyEvaluationError(f"OPA eval failed ({rego_package}): {result.stderr}")
        output = json.loads(result.stdout)
        return bool(output.get('result', [{}])[0].get('expressions', [{}])[0].get('value', False))
    except PolicyEvaluationError:
        raise
    except Exception as e:
        raise PolicyEvaluationError(f"OPA exception ({rego_package}): {e}")


# --- Exception Query ---

class ExceptionQueryError(Exception):
    """Raised when the exceptions table query fails (vs. no exception found)."""
    pass


def query_exception(control_id: str, resource_arn: str) -> Optional[Dict[str, Any]]:
    """Return the active exception for the resource/control, or None if none exists.

    Raises ExceptionQueryError on query failure — the caller must NOT treat a
    failed query as 'no exception', because that would remediate a resource
    that may have a valid approved exception (fail-secure for compliance, but
    destructive for an authorized resource). On query failure, escalate.
    """
    try:
        # No Limit here: DynamoDB applies Limit BEFORE FilterExpression, so
        # Limit=1 could read only an expired exception and miss a valid one
        # for the same resource+control pair.
        resp = exceptions_table.query(
            IndexName='resource-control-index',
            KeyConditionExpression='resource_arn = :arn AND control_id = :cid',
            FilterExpression='valid_until > :now',
            ExpressionAttributeValues={
                ':arn': resource_arn, ':cid': control_id, ':now': int(time.time())
            }
        )
        items = resp.get('Items', [])
        return items[0] if items else None
    except Exception as e:
        print(f"DynamoDB exception query error: {e}")
        raise ExceptionQueryError(str(e))


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
    return resource_arn.split(':')[-1].split('/')[-1]


# --- Handler ---

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    print(f"Received event: {json.dumps(event)}")

    resource_arn = event['resource_arn']
    account_id = event['account_id']
    region = event['region']
    resource_id = extract_resource_id(resource_arn)
    now = canonical_timestamp()

    # Deserialize controls from DynamoDB typed format
    raw_controls = event['controls']
    controls = [deserialize_item(c) for c in raw_controls]

    _config_cache.clear()
    results = []

    for control in controls:
        control_id = control['control_id']
        try:
            rego_package = control['rego_policy_package']
            policy_version = f"{rego_package}:{control.get('policy_version', 'v1.0')}"

            fetch_config = control.get('fetch_config')
            if not fetch_config:
                raise ValueError(f"No fetch_config for: {control_id}")

            resource_config = fetch_resource_config(fetch_config, resource_id)
            is_compliant = evaluate_policy_with_opa(
                rego_package, resource_config,
                control.get('rego_s3_key'), control.get('policy_version', 'v1.0'))

            # Only query exceptions for non-compliant resources. A query failure
            # here must escalate (S_error), never silently remediate an
            # authorized resource.
            exception = None
            if not is_compliant:
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
