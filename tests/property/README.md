# Property-Based Tests

This directory contains property-based tests using Hypothesis to validate correctness properties of the compliance automation system.

## Overview

Property-based testing validates universal properties that should hold for all possible inputs, rather than testing specific examples. Each test generates 100+ random test cases to verify the property.

## Test Files

### test_analyze_properties.py

Property tests for the Analyze Lambda component.

**Properties Tested:**

1. **Property 3: Analyze Classification Determinism** (Requirements 3.9, 16.3)
   - Validates that identical inputs always produce identical classification
   - Tests with random S3 configurations and exception states
   - Ensures no non-deterministic behavior in classification logic

2. **Property 4: Compliant Resource Classification** (Requirements 3.3, 10.7)
   - Validates that resources with all four public access blocks enabled are classified as S_nominal
   - Tests all compliant configurations
   - Ensures policy correctly identifies compliant state

3. **Property 5: Valid Exception Classification** (Requirements 3.4, 3.7)
   - Validates that non-compliant resources with valid exceptions are classified as S_exception
   - Tests with random non-compliant configs and future-dated exceptions
   - Ensures exception logic correctly prevents remediation

4. **Property 6: Violation Classification** (Requirements 3.5, 3.8)
   - Validates that non-compliant resources without valid exceptions are classified as S_violation
   - Tests with expired exceptions and no exceptions
   - Ensures violations are correctly identified for remediation

5. **Property 8: Exception Query Correctness** (Requirements 3.6, 5.8)
   - Validates that exception queries use correct GSI and filters
   - Tests query logic with various exception states
   - Ensures only valid exceptions (control_id match, resource_arn match, valid_until > now) are returned

6. **Integration Property: End-to-End Classification**
   - Validates complete classification flow from event to result
   - Tests all combinations of compliance states and exception states
   - Ensures correct state transitions and data flow

## Running the Tests

### Run all property tests:
```bash
pytest tests/property/ -v
```

### Run specific property test:
```bash
pytest tests/property/test_analyze_properties.py::test_property_3_classification_determinism -v
```

### Run with coverage:
```bash
pytest tests/property/ --cov=lambdas/analyze --cov-report=html
```

### Increase iterations for more thorough testing:
```bash
pytest tests/property/ -v --hypothesis-seed=random --hypothesis-show-statistics
```

## Test Configuration

- **Minimum iterations per property**: 100 (configured via `@settings(max_examples=100)`)
- **Random seed**: Hypothesis uses deterministic random seed by default for reproducibility
- **Shrinking**: Hypothesis automatically shrinks failing examples to minimal cases

## Hypothesis Strategies

The tests use custom Hypothesis strategies to generate valid test data:

- `s3_public_access_config()`: Random S3 public access block configurations
- `compliant_s3_config()`: Configurations with all settings True
- `non_compliant_s3_config()`: Configurations with at least one setting False
- `bucket_name_strategy()`: Valid S3 bucket names
- `resource_arn_strategy()`: Valid S3 resource ARNs
- `exception_item_strategy()`: DynamoDB exception items with valid/expired timestamps
- `analyze_event_strategy()`: Valid Analyze Lambda input events

## Requirements Coverage

These property tests validate the following requirements:

- **3.3**: Compliant resource classification
- **3.4**: Valid exception classification
- **3.5**: Violation classification
- **3.6**: Exception query via GSI with filters
- **3.7**: Valid exception timestamp check
- **3.8**: Expired exception handling
- **3.9**: Deterministic classification
- **5.8**: Exception filtering by valid_until
- **10.7**: Policy evaluation for compliant state
- **16.3**: Consistent timestamp usage

## Debugging Failed Properties

When a property test fails, Hypothesis will:

1. Show the failing example
2. Attempt to shrink it to a minimal failing case
3. Print the seed for reproduction

To reproduce a failure:
```bash
pytest tests/property/test_analyze_properties.py::test_name --hypothesis-seed=<seed>
```

## Adding New Properties

To add a new property test:

1. Define the property in a docstring (GIVEN/WHEN/THEN format)
2. List the requirements it validates
3. Use `@settings(max_examples=100)` decorator
4. Use `@given()` with appropriate Hypothesis strategies
5. Write assertions that validate the property
6. Add documentation to this README
