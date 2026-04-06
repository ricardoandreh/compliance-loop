# Analyze Lambda

Lambda function that classifies S3 bucket compliance state using OPA/Rego policy evaluation.

## Components

- `handler.py`: Main Lambda handler
- `s3_public_access.rego`: Rego policy for S3 public access evaluation
- `requirements.txt`: Python dependencies

## Deployment Requirements

### OPA Binary

The Lambda requires the OPA binary to be included in the deployment package. Download and include it:

```bash
# Download OPA binary for Linux (Lambda runtime)
curl -L -o lambdas/analyze/opa https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static
chmod +x lambdas/analyze/opa
```

### Environment Variables

The Lambda expects the following environment variables:

- `EXCEPTIONS_TABLE`: DynamoDB exceptions table name (default: `exceptions`)
- `VIOLATIONS_LOG_TABLE`: DynamoDB violations_log table name (default: `violations_log`)

### IAM Permissions

The Lambda execution role requires:

- `s3:GetPublicAccessBlock` on target buckets
- `dynamodb:Query` on exceptions table (control_id-index GSI)
- `dynamodb:PutItem` on violations_log table
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`

## Deployment Package Structure

```
analyze-lambda.zip
├── handler.py
├── s3_public_access.rego
├── opa (binary)
└── (Python dependencies from requirements.txt)
```

## Testing Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export EXCEPTIONS_TABLE=exceptions
export VIOLATIONS_LOG_TABLE=violations_log
export AWS_REGION=us-east-1

# Test with sample event
python -c "
import handler
import json

event = {
    'resource_arn': 'arn:aws:s3:::test-bucket',
    'control_id': 'ctrl-s3-public-block',
    'account_id': '123456789012',
    'region': 'us-east-1',
    'event_time': '2024-01-15T10:30:00Z'
}

result = handler.lambda_handler(event, None)
print(json.dumps(result, indent=2))
"
```

## State Classification Logic

1. **S_nominal**: Resource is compliant with policy
2. **S_exception**: Resource is non-compliant but has valid exception
3. **S_violation**: Resource is non-compliant without valid exception

## Error Handling

- **S3 GetPublicAccessBlock fails**: Exception propagates to Step Functions
- **OPA evaluation error**: Fail-secure, returns non-compliant (S_violation)
- **DynamoDB query timeout**: Fail-secure, proceeds without exception
- **violations_log write fails**: Exception propagates to Step Functions
