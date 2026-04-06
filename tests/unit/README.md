# Unit Tests for Analyze Lambda

## Overview

This directory contains comprehensive unit tests for the Analyze Lambda handler, covering all critical functionality and error handling scenarios.

## Test Coverage

### Test Classes

1. **TestExtractBucketName**: Tests bucket name extraction from S3 ARNs
2. **TestGetBucketPublicAccessConfig**: Tests S3 public access block configuration retrieval
3. **TestEvaluatePolicyWithOPA**: Tests OPA policy evaluation with error handling
4. **TestQueryException**: Tests DynamoDB exception querying with fail-secure behavior
5. **TestClassifyState**: Tests state classification logic
6. **TestLogViolation**: Tests violations_log writing
7. **TestLambdaHandlerCompliantResource**: Tests S_nominal classification
8. **TestLambdaHandlerNonCompliantWithValidException**: Tests S_exception classification
9. **TestLambdaHandlerNonCompliantWithoutException**: Tests S_violation classification
10. **TestLambdaHandlerExpiredException**: Tests expired exception handling
11. **TestLambdaHandlerGetPublicAccessBlockError**: Tests S3 API error propagation
12. **TestLambdaHandlerOPAError**: Tests fail-secure behavior for OPA errors
13. **TestLambdaHandlerDynamoDBTimeout**: Tests fail-secure behavior for DynamoDB timeouts

## Requirements Validated

- **Requirement 3.3**: Compliant resource returns S_nominal
- **Requirement 3.4**: Non-compliant resource with valid exception returns S_exception
- **Requirement 3.5**: Non-compliant resource without exception returns S_violation
- **Requirement 3.7**: Valid exception is recognized when valid_until > now()
- **Requirement 3.8**: Expired exception is ignored when valid_until <= now()

## Running the Tests

### Prerequisites

Install required dependencies:

```bash
pip install -r lambdas/requirements.txt
```

### Run All Tests

Using unittest:
```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)/lambdas/analyze"
python3 -m unittest tests.unit.test_analyze_lambda -v
```

Using pytest:
```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)/lambdas/analyze"
pytest tests/unit/test_analyze_lambda.py -v
```

Using the provided script:
```bash
./run_analyze_tests.sh
```

### Run Specific Test Class

```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)/lambdas/analyze"
python3 -m unittest tests.unit.test_analyze_lambda.TestLambdaHandlerCompliantResource -v
```

### Run with Coverage

```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)/lambdas/analyze"
pytest tests/unit/test_analyze_lambda.py --cov=handler --cov-report=html
```

## Test Scenarios

### 1. Compliant Resource (S_nominal)
- All four public access blocks enabled
- OPA evaluation returns True
- No exception needed
- Expected: state = "S_nominal"

### 2. Non-Compliant with Valid Exception (S_exception)
- One or more public access blocks disabled
- OPA evaluation returns False
- Valid exception exists (valid_until > now())
- Expected: state = "S_exception", exception_id populated

### 3. Non-Compliant without Exception (S_violation)
- One or more public access blocks disabled
- OPA evaluation returns False
- No exception exists
- Expected: state = "S_violation"

### 4. Expired Exception
- Non-compliant configuration
- Exception exists but valid_until <= now()
- DynamoDB query filters out expired exception
- Expected: state = "S_violation"

### 5. GetPublicAccessBlock Error
- S3 API call fails (NoSuchBucket, AccessDenied, etc.)
- Expected: Exception propagates to Step Functions

### 6. OPA Evaluation Error (Fail-Secure)
- OPA subprocess fails or times out
- Expected: Returns False (non-compliant), state = "S_violation"

### 7. DynamoDB Query Timeout (Fail-Secure)
- DynamoDB query fails or times out
- Expected: Returns None (no exception), proceeds with classification

## Mocking Strategy

All tests use `unittest.mock` to isolate the handler logic:

- **boto3 clients**: Mocked to avoid AWS API calls
- **subprocess.run**: Mocked to simulate OPA evaluation
- **time.time()**: Mocked for deterministic timestamp testing
- **uuid.uuid4()**: Mocked for predictable violation IDs

## Fail-Secure Behavior

The tests validate critical fail-secure patterns:

1. **OPA errors** → Return False (treat as violation)
2. **DynamoDB errors** → Return None (proceed without exception)
3. **S3 errors** → Propagate exception (require manual intervention)

This ensures the system defaults to the secure state when components fail.

## Notes

- Tests do not require actual AWS resources
- All AWS interactions are mocked
- Tests run in isolation without side effects
- Each test class focuses on a specific component or scenario
