# Step Functions State Machine - Compliance Loop

## Overview

This document describes the AWS Step Functions state machine that orchestrates the MAPE-K compliance automation loop. The state machine coordinates the Monitor, Analyze, Plan, and Execute components to detect, classify, and remediate compliance violations.

**Query Language**: This state machine uses **JSONata** instead of JSONPath for more powerful data transformation and manipulation capabilities.

## JSONata vs JSONPath

This state machine uses **JSONata** as the query language instead of the traditional JSONPath. Key differences based on [AWS Step Functions JSONata documentation](https://docs.aws.amazon.com/step-functions/latest/dg/transforming-data.html):

### JSONPath (Traditional)
```json
{
  "Parameters": {
    "value.$": "$.some.path"
  },
  "ResultPath": "$.result"
}
```

### JSONata (This Implementation)
```json
{
  "QueryLanguage": "JSONata",
  "Arguments": {
    "value": "{% $states.input.some.path %}"
  },
  "Output": {
    "result": "{% $states.result %}"
  }
}
```

### JSONata Advantages

1. **Simpler I/O model**: Only 2 fields (Arguments, Output) instead of 5 (InputPath, Parameters, ResultSelector, ResultPath, OutputPath)
2. **More expressive transformations**: Can construct complex objects inline
3. **String concatenation**: `'Error: ' & $error.message`
4. **Function calls**: `$string()`, `$uuid()`, `$parse()`
5. **No more `.$` suffix**: Clean field names without special syntax

### Reserved Variable: $states

Step Functions provides the `$states` reserved variable with the following structure:

```jsonata
$states = {
  "input":       // Original input to the state
  "result":      // API or sub-workflow's result (if successful)
  "errorOutput": // Error Output (only available in a Catch)
  "context":     // Context object (execution metadata)
}
```

### JSONata Syntax Used

- **`$states.input`**: Reference to current state input (replaces `$` in JSONPath)
- **`$states.input.field`**: Access field from input (replaces `$.field`)
- **`$states.result`**: Access task/API result (replaces ResultSelector)
- **`$states.errorOutput`**: Error information in catch blocks
- **`$states.context`**: Execution context (replaces `$$`)
- **`$states.context.State.EnteredTime`**: When state was entered (replaces `$$.State.EnteredTime`)
- **`$states.context.Execution.Id`**: Execution ARN (replaces `$$.Execution.Id`)
- **`{% expression %}`**: JSONata expression block (must start with `{%` and end with `%}`)
- **`$string()`**: Convert value to string
- **`$uuid()`**: Generate UUID (replaces `States.UUID()`)
- **`&`**: String concatenation operator

### Key Differences from Initial Implementation

1. **Use `$states.input` not `%`**: The `%` syntax was incorrect
2. **Use `$states.result` for task outputs**: Not `%.Payload`
3. **Use `$states.context` for execution metadata**: Not `%.$states`
4. **All expressions must be in `{% %}`**: Even simple field references
5. **No `.$` suffix on field names**: Clean JSON object keys

## Architecture

The state machine implements the following workflow:

1. **InvokeAnalyze**: Calls the Analyze Lambda to classify resource compliance state
2. **ClassifyState**: Routes execution based on the classification (S_nominal, S_exception, S_violation)
3. **RegisterNominal/RegisterException**: Logs compliant or excepted resources to DynamoDB
4. **InvokeExecute**: Calls the Execute Lambda to remediate violations
5. **CheckRemediationStatus**: Determines if remediation succeeded or failed
6. **UpdateLogSuccess/UpdateLogFailed**: Updates the violations log with remediation outcome
7. **EnqueueEscalation**: Sends failed remediations to the escalation queue for human intervention

## State Definitions

### InvokeAnalyze

**Type**: Task (Lambda invocation)

**Purpose**: Classify the compliance state of the resource

**Timeout**: 15 seconds

**Input**:
```json
{
  "resource_arn": "arn:aws:s3:::bucket-name",
  "resource_id": "bucket-name",
  "control_id": "ctrl-s3-public-block",
  "account_id": "123456789012",
  "region": "us-east-1",
  "event_time": "2024-01-15T10:30:00Z"
}
```

**Arguments** (using JSONata):
```json
{
  "FunctionName": "${analyze_lambda_arn}",
  "Payload": {
    "resource_arn": "{% $states.input.resource_arn %}",
    "resource_id": "{% $states.input.resource_id %}",
    "control_id": "{% $states.input.control_id %}",
    "account_id": "{% $states.input.account_id %}",
    "region": "{% $states.input.region %}",
    "event_time": "{% $states.input.event_time %}"
  }
}
```

**Output** (JSONata transformation):
```json
{
  "resource_arn": "{% $states.input.resource_arn %}",
  "resource_id": "{% $states.input.resource_id %}",
  "control_id": "{% $states.input.control_id %}",
  "account_id": "{% $states.input.account_id %}",
  "region": "{% $states.input.region %}",
  "event_time": "{% $states.input.event_time %}",
  "analyze_result": {
    "state": "{% $states.result.Payload.state %}",
    "violation_id": "{% $states.result.Payload.violation_id %}",
    "exception_id": "{% $states.result.Payload.exception_id %}",
    "approved_by": "{% $states.result.Payload.approved_by %}",
    "timestamp": "{% $states.result.Payload.timestamp %}",
    "policy_version": "{% $states.result.Payload.policy_version %}"
  }
}
```

**Error Handling**:
- Retries: 3 attempts with exponential backoff (2s initial, 2x multiplier)
- Catch: All errors route to AnalyzeFailedEscalation state
- Catch Output: Preserves input fields and adds error using `$states.errorOutput`

### ClassifyState

**Type**: Choice

**Purpose**: Route execution based on compliance classification

**Branches** (using JSONata conditions):
- `{% $states.input.analyze_result.state = 'S_nominal' %}` → RegisterNominal (resource is compliant)
- `{% $states.input.analyze_result.state = 'S_exception' %}` → RegisterException (resource has valid exception)
- `{% $states.input.analyze_result.state = 'S_violation' %}` → InvokeExecute (resource needs remediation)
- Default → UnknownStateEscalation (unexpected state value)

**Note**: JSONata uses `=` for equality comparison within `{% %}` expression blocks.

### RegisterNominal

**Type**: Task (DynamoDB PutItem)

**Purpose**: Log compliant resource evaluation to violations_log

**Arguments** (using JSONata for field references):
```json
{
  "TableName": "${violations_log_table}",
  "Item": {
    "violation_id": {"S": "{% $states.input.analyze_result.violation_id %}"},
    "resource_arn": {"S": "{% $states.input.resource_arn %}"},
    "control_id": {"S": "{% $states.input.control_id %}"},
    "state": {"S": "S_nominal"},
    "timestamp": {"S": "{% $states.input.analyze_result.timestamp %}"},
    "policy_version": {"S": "{% $states.input.analyze_result.policy_version %}"},
    "account_id": {"S": "{% $states.input.account_id %}"},
    "region": {"S": "{% $states.input.region %}"}
  }
}
```

**Next**: End (workflow terminates)

### RegisterException

**Type**: Task (DynamoDB PutItem)

**Purpose**: Log excepted resource evaluation to violations_log

**DynamoDB Item**:
```json
{
  "violation_id": "550e8400-e29b-41d4-a716-446655440000",
  "resource_arn": "arn:aws:s3:::bucket-name",
  "control_id": "ctrl-s3-public-block",
  "state": "S_exception",
  "timestamp": "2024-01-15T10:30:15Z",
  "policy_version": "s3_public_access:v1.0",
  "exception_id": "exc-a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "approved_by": "security-lead@example.com",
  "account_id": "123456789012",
  "region": "us-east-1"
}
```

**Next**: End (workflow terminates without remediation)

### InvokeExecute

**Type**: Task (Lambda invocation)

**Purpose**: Remediate non-compliant resource using OpenTofu

**Timeout**: 45 seconds

**Input**:
```json
{
  "resource_arn": "arn:aws:s3:::bucket-name",
  "resource_id": "bucket-name",
  "region": "us-east-1",
  "violation_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Output**:
```json
{
  "remediation_status": "success",
  "output": "aws_s3_bucket_public_access_block.remediation: Creation complete",
  "error": null
}
```

**Error Handling**:
- Retries: 2 attempts with exponential backoff (5s initial, 2x multiplier)
- Catch: All errors route to UpdateLogFailed state

### CheckRemediationStatus

**Type**: Choice

**Purpose**: Determine remediation outcome

**Branches**:
- `remediation_status == "success"` → UpdateLogSuccess
- `remediation_status == "failed"` → UpdateLogFailed
- Default → UpdateLogFailed (treat unknown status as failure)

### UpdateLogSuccess

**Type**: Task (DynamoDB UpdateItem)

**Purpose**: Update violations_log with successful remediation

**DynamoDB Update**:
```
SET remediation_status = "success",
    remediation_timestamp = "2024-01-15T10:30:45Z",
    remediation_output = "aws_s3_bucket_public_access_block.remediation: Creation complete"
```

**Next**: End (workflow terminates successfully)

### UpdateLogFailed

**Type**: Task (DynamoDB UpdateItem)

**Purpose**: Update violations_log with failed remediation

**DynamoDB Update**:
```
SET remediation_status = "failed",
    remediation_timestamp = "2024-01-15T10:30:45Z",
    remediation_error = "AccessDenied: Insufficient permissions"
```

**Error Handling**:
- Retries: 3 attempts for throttling errors (1s initial, 2x multiplier)

**Next**: EnqueueEscalation

### EnqueueEscalation

**Type**: Task (SQS SendMessage)

**Purpose**: Send failed remediation to escalation queue for human intervention

**Arguments** (using JSONata for message construction):
```json
{
  "QueueUrl": "${escalation_queue_url}",
  "MessageBody": "{% $string({...}) %}",
  "MessageGroupId": "{% $states.input.resource_arn %}",
  "MessageDeduplicationId": "{% $states.input.analyze_result.violation_id %}"
}
```

**MessageBody JSONata Expression**:
```jsonata
$string({
  'violation_id': $states.input.analyze_result.violation_id,
  'resource_arn': $states.input.resource_arn,
  'control_id': $states.input.control_id,
  'error': $string($states.input.execute_result.error),
  'timestamp': $states.context.State.EnteredTime,
  'step_functions_execution_arn': $states.context.Execution.Id,
  'account_id': $states.input.account_id,
  'region': $states.input.region
})
```

**FIFO Configuration**:
- MessageGroupId: `{% $states.input.resource_arn %}` (ensures ordered processing per resource)
- MessageDeduplicationId: `{% $states.input.analyze_result.violation_id %}` (prevents duplicate escalations)

**Error Handling**:
- Retries: 3 attempts with exponential backoff (2s initial, 2x multiplier)

**Next**: End (workflow terminates after escalation)

**Note**: JSONata's `$string()` function converts the object to JSON string for SQS MessageBody. Context metadata accessed via `$states.context`.

### AnalyzeFailedEscalation

**Type**: Task (SQS SendMessage)

**Purpose**: Escalate when Analyze Lambda fails

**Trigger**: Catch block from InvokeAnalyze state

**MessageBody JSONata Expression**:
```jsonata
$string({
  'violation_id': $uuid(),
  'resource_arn': $states.input.resource_arn,
  'control_id': $states.input.control_id,
  'error': 'Analyze Lambda failed: ' & $string($states.input.error),
  'timestamp': $states.context.State.EnteredTime,
  'step_functions_execution_arn': $states.context.Execution.Id,
  'account_id': $states.input.account_id,
  'region': $states.input.region
})
```

**MessageDeduplicationId**: `{% $uuid() %}` (generates unique ID for each failure)

**Next**: End

**Note**: Uses JSONata string concatenation (`&`) to build error message. Error details from catch block available in `$states.input.error`.

### UnknownStateEscalation

**Type**: Task (SQS SendMessage)

**Purpose**: Escalate when Analyze returns unexpected state value

**Trigger**: Default branch from ClassifyState

**MessageBody JSONata Expression**:
```jsonata
$string({
  'violation_id': $states.input.analyze_result.violation_id,
  'resource_arn': $states.input.resource_arn,
  'control_id': $states.input.control_id,
  'error': 'Unknown state returned by Analyze: ' & $states.input.analyze_result.state,
  'timestamp': $states.context.State.EnteredTime,
  'step_functions_execution_arn': $states.context.Execution.Id,
  'account_id': $states.input.account_id,
  'region': $states.input.region
})
```

**Next**: End

**Note**: Uses JSONata string concatenation to include the unexpected state value in the error message.

## Timeout Configuration

- **Global timeout**: 60 seconds (entire workflow must complete within this time)
- **InvokeAnalyze timeout**: 15 seconds
- **InvokeExecute timeout**: 45 seconds

If any timeout is exceeded, the workflow terminates and the error is logged.

## Error Handling Strategy

### Retry Policies

1. **Lambda invocations**: Retry on service exceptions (3 attempts for Analyze, 2 for Execute)
2. **DynamoDB operations**: Retry on throttling (3 attempts)
3. **SQS operations**: Retry on service exceptions (3 attempts)

### Catch Policies

1. **InvokeAnalyze**: Catch all errors → AnalyzeFailedEscalation
2. **InvokeExecute**: Catch all errors → UpdateLogFailed
3. **ClassifyState**: Default branch → UnknownStateEscalation

### Fail-Secure Principles

- Unknown exception state → Treat as violation (remediate)
- Unknown remediation status → Treat as failure (escalate)
- Analyze error → Escalate (don't assume compliant)
- Execute error → Escalate (don't assume remediated)

## State Transitions

```
CloudTrail Event
    ↓
InvokeAnalyze
    ↓
ClassifyState
    ├─→ S_nominal → RegisterNominal → END
    ├─→ S_exception → RegisterException → END
    └─→ S_violation → InvokeExecute
                          ↓
                    CheckRemediationStatus
                          ├─→ success → UpdateLogSuccess → END
                          └─→ failed → UpdateLogFailed → EnqueueEscalation → END
```

## Deployment

The state machine definition is stored in `state_machine_definition.json` and deployed via Terraform using the `templatefile` function to inject resource ARNs and names.

**Query Language**: The state machine uses `"QueryLanguage": "JSONata"` to enable JSONata expressions throughout.

**Template Variables**:
- `analyze_lambda_arn`: ARN of the Analyze Lambda function
- `execute_lambda_arn`: ARN of the Execute Lambda function
- `violations_log_table`: Name of the violations_log DynamoDB table
- `escalation_queue_url`: URL of the escalation SQS FIFO queue

**Key JSONata Features Used**:
- Field references: `{% $states.input.field %}` (state input)
- Task results: `{% $states.result.Payload.field %}` (Lambda response)
- Context objects: `{% $states.context.State.EnteredTime %}`, `{% $states.context.Execution.Id %}`
- Error handling: `{% $states.errorOutput %}` (in catch blocks)
- String concatenation: `'text' & $states.input.field`
- Function calls: `$string()`, `$uuid()`
- Complex object construction: All expressions wrapped in `{% %}`

**Important Notes**:
- All JSONata expressions must be wrapped in `{% %}` delimiters
- Use `$states.input` to access state input (not `%` or `$`)
- Use `$states.result` to access task/API results
- Use `$states.context` to access execution metadata
- No `.$` suffix on field names in Arguments/Output

## Monitoring

The state machine is configured with CloudWatch Logs integration:
- Log level: ALL (includes execution data)
- Log group: `/aws/vendedlogs/states/${project_name}-compliance-loop-${environment}`
- Retention: 30 days

## Requirements Validation

This implementation satisfies the following requirements:

- **Requirement 2.1**: Orchestrator invokes Analyze with event metadata
- **Requirement 2.2**: S_nominal terminates without invoking Execute
- **Requirement 2.3**: S_exception terminates without invoking Execute
- **Requirement 2.4**: S_violation invokes Execute
- **Requirement 2.5**: Execute success terminates workflow
- **Requirement 2.6**: Execute failure invokes escalation mechanism
- **Requirement 2.7**: Entire loop completes within 60 seconds
- **Requirement 17.3**: Global timeout configured at 60 seconds

## Testing

The state machine can be tested using:

1. **Unit tests**: Validate ASL JSON structure (task 7.2)
2. **Property tests**: Verify state-based routing (task 7.3)
3. **Integration tests**: End-to-end scenarios C1-C4 (task 14)

## Future Enhancements

- Add X-Ray tracing for distributed tracing
- Implement parallel execution for multiple resources
- Add custom metrics for compliance monitoring
- Implement circuit breaker pattern for Execute failures
