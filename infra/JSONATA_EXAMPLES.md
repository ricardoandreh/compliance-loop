# JSONata Examples for Step Functions

This document provides examples of JSONata expressions used in the Compliance Loop state machine.

## Basic Syntax

### Field Access

**JSONPath (old)**:
```json
"field.$": "$.some.nested.field"
```

**JSONata (new)**:
```json
"field": "{% $states.input.some.nested.field %}"
```

### Context Object Access

**JSONPath (old)**:
```json
"timestamp.$": "$$.State.EnteredTime",
"executionId.$": "$$.Execution.Id"
```

**JSONata (new)**:
```json
"timestamp": "{% $states.context.State.EnteredTime %}",
"executionId": "{% $states.context.Execution.Id %}"
```

## Complex Transformations

### Object Construction

**JSONPath (old)** - requires ResultSelector:
```json
"ResultPath": "$.analyze_result",
"ResultSelector": {
  "state.$": "$.Payload.state",
  "violation_id.$": "$.Payload.violation_id"
}
```

**JSONata (new)** - inline transformation:
```json
"Output": {
  "analyze_result": {
    "state": "{% $states.result.Payload.state %}",
    "violation_id": "{% $states.result.Payload.violation_id %}"
  }
}
```

### Merging Objects

**JSONPath (old)** - not directly supported:
```json
"ResultPath": "$.error"
```

**JSONata (new)** - preserve all fields and add error:
```json
"Output": {
  "resource_arn": "{% $states.input.resource_arn %}",
  "control_id": "{% $states.input.control_id %}",
  "error": "{% $states.errorOutput %}"
}
```

Note: In JSONata with Step Functions, you explicitly construct the output object rather than using merge functions.

## String Operations

### String Concatenation

**JSONPath (old)** - requires States.Format:
```json
"error.$": "States.Format('Analyze failed: {}', $.error.message)"
```

**JSONata (new)**:
```json
"error": "{% 'Analyze failed: ' & $string($states.input.error.message) %}"
```

### Type Conversion

**JSONata**:
```jsonata
$string(%.some.value)  // Convert to string
$number(%.some.value)  // Convert to number
$boolean(%.some.value) // Convert to boolean
```

## Conditional Logic

### Choice State Conditions

**JSONPath (old)**:
```json
{
  "Variable": "$.state",
  "StringEquals": "S_nominal"
}
```

**JSONata (new)**:
```json
{
  "Condition": "{% $states.input.state = 'S_nominal' %}"
}
```

### Comparison Operators

```jsonata
$states.input.value = 'text'      // Equality
$states.input.value != 'text'     // Inequality
$states.input.number > 10         // Greater than
$states.input.number >= 10        // Greater than or equal
$states.input.number < 10         // Less than
$states.input.number <= 10        // Less than or equal
```

### Logical Operators

```jsonata
$states.input.a = 'x' and $states.input.b = 'y'   // AND
$states.input.a = 'x' or $states.input.b = 'y'    // OR
not($states.input.a = 'x')                         // NOT
```

## Built-in Functions

### UUID Generation

**JSONPath (old)**:
```json
"id.$": "States.UUID()"
```

**JSONata (new)**:
```json
"id": "{% $states.uuid() %}"
```

### Array Operations

```jsonata
$count($states.input.items)                    // Count array elements
$sum($states.input.numbers)                    // Sum numeric array
$max($states.input.numbers)                    // Maximum value
$min($states.input.numbers)                    // Minimum value
$append($states.input.array, 'item')           // Append to array
```

### Object Operations

```jsonata
$keys($states.input.object)                    // Get object keys
$exists($states.input.field)                   // Check if field exists
```

## Real Examples from State Machine

### 1. InvokeAnalyze Output Transformation

Transforms Lambda response into structured state:

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

### 2. Error Handling in Catch Block

Preserves input fields and adds error information:

```json
{
  "resource_arn": "{% $states.input.resource_arn %}",
  "resource_id": "{% $states.input.resource_id %}",
  "control_id": "{% $states.input.control_id %}",
  "account_id": "{% $states.input.account_id %}",
  "region": "{% $states.input.region %}",
  "error": "{% $states.errorOutput %}"
}
```

### 3. SQS Message Construction

Builds JSON message body for escalation:

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

The outer `$string()` converts the object to JSON string for SQS.

### 4. Error Message with Concatenation

Builds descriptive error message:

```jsonata
'Analyze Lambda failed: ' & $string($states.input.error)
```

### 5. DynamoDB UpdateExpression Value

Converts error object to string for storage:

```jsonata
$string($states.input.execute_result.error)
```

## Context Variables

JSONata provides access to Step Functions context through the reserved `$states` variable:

```jsonata
$states.input                      // Original input to the state
$states.result                     // API or sub-workflow's result (if successful)
$states.errorOutput                // Error Output (only available in a Catch)
$states.context                    // Context object with execution metadata
$states.context.State.EnteredTime  // When state was entered (ISO 8601)
$states.context.Execution.Id       // Execution ARN
$states.context.StateMachine.Id    // State machine ARN
```

**Important**: 
- Use `$states.input` to access state input (not `%` or `$`)
- Use `$states.result` to access task/API results
- Use `$states.context` to access execution metadata
- `$states.errorOutput` is only available in Catch blocks

## Best Practices

### 1. Use Expression Blocks for All References

All JSONata expressions must be wrapped in `{% ... %}`:

```json
"Output": {
  "result": "{% $states.result.value %}"
}
```

### 2. Always Convert to String for SQS MessageBody

SQS requires string message body:

```json
"MessageBody": "{% $string({...}) %}"
```

### 3. Handle Null Values

Use `$exists()` to check for field existence:

```jsonata
$exists($states.input.optional_field) ? $states.input.optional_field : 'default'
```

### 4. Preserve State Context

When transforming output, preserve necessary fields explicitly:

```json
{
  "original_field": "{% $states.input.original_field %}",
  "new_field": "{% $states.result.transformed.value %}"
}
```

### 5. Use Descriptive Error Messages

Concatenate context into error messages:

```jsonata
'Failed to process ' & $states.input.resource_arn & ': ' & $string($states.input.error)
```

## Migration from JSONPath

| JSONPath | JSONata | Notes |
|----------|---------|-------|
| `$.field` | `{% $states.input.field %}` | Field access from state input |
| `$$.State.EnteredTime` | `{% $states.context.State.EnteredTime %}` | Context object |
| `States.Format()` | `'text' & $states.input.field` | String concatenation |
| `States.UUID()` | `$uuid()` | UUID generation |
| `ResultPath` | `Output` field | Output transformation |
| `ResultSelector` | Inline in `Output` | Object construction |
| `Parameters` | `Arguments` | Task input |
| `field.$` | `field` (no suffix) | Clean field names |

## References

- [JSONata Documentation](https://jsonata.org/)
- [AWS Step Functions JSONata Support](https://docs.aws.amazon.com/step-functions/latest/dg/transforming-data.html)
- [JSONata Exerciser](https://try.jsonata.org/) - Interactive playground
