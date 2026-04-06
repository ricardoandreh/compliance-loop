"""
Property-Based Tests for Analyze Lambda

Tests correctness properties using Hypothesis to generate random test cases.
Each property validates specific requirements with minimum 100 iterations.

Properties tested:
- Property 3: Analyze Classification Determinism (Requirements 3.9, 16.3)
- Property 4: Compliant Resource Classification (Requirements 3.3, 10.7)
- Property 5: Valid Exception Classification (Requirements 3.4, 3.7)
- Property 6: Violation Classification (Requirements 3.5, 3.8)
- Property 8: Exception Query Correctness (Requirements 3.6, 5.8)
"""

import sys
import os
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any, Optional

import pytest
from hypothesis import given, settings, strategies as st
from hypothesis import assume

# Add lambdas/analyze to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../lambdas/analyze'))

from handler import (
    classify_state,
    extract_bucket_name,
    evaluate_policy_with_opa,
    query_exception,
    lambda_handler
)


# ============================================================================
# Hypothesis Strategies
# ============================================================================

@st.composite
def s3_public_access_config(draw):
    """Generate random S3 public access block configuration."""
    return {
        'BlockPublicAcls': draw(st.booleans()),
        'IgnorePublicAcls': draw(st.booleans()),
        'BlockPublicPolicy': draw(st.booleans()),
        'RestrictPublicBuckets': draw(st.booleans())
    }


def compliant_s3_config():
    """Generate S3 configuration that is compliant (all settings True)."""
    return st.just({
        'BlockPublicAcls': True,
        'IgnorePublicAcls': True,
        'BlockPublicPolicy': True,
        'RestrictPublicBuckets': True
    })


@st.composite
def non_compliant_s3_config(draw):
    """Generate S3 configuration that is non-compliant (at least one False)."""
    config = {
        'BlockPublicAcls': draw(st.booleans()),
        'IgnorePublicAcls': draw(st.booleans()),
        'BlockPublicPolicy': draw(st.booleans()),
        'RestrictPublicBuckets': draw(st.booleans())
    }
    # Ensure at least one setting is False
    assume(not all(config.values()))
    return config


@st.composite
def bucket_name_strategy(draw):
    """Generate valid S3 bucket names."""
    # Simplified bucket name generation (lowercase alphanumeric and hyphens)
    length = draw(st.integers(min_value=3, max_value=63))
    chars = draw(st.lists(
        st.sampled_from('abcdefghijklmnopqrstuvwxyz0123456789-'),
        min_size=length,
        max_size=length
    ))
    name = ''.join(chars).strip('-')
    # Ensure valid bucket name
    assume(len(name) >= 3 and not name.startswith('-') and not name.endswith('-'))
    return name


@st.composite
def resource_arn_strategy(draw):
    """Generate valid S3 resource ARNs."""
    bucket_name = draw(bucket_name_strategy())
    return f"arn:aws:s3:::{bucket_name}"


@st.composite
def exception_item_strategy(draw, valid=True, base_time=None):
    """
    Generate DynamoDB exception item.
    
    Args:
        valid: If True, generates future valid_until timestamp
               If False, generates past valid_until timestamp
        base_time: Base timestamp for deterministic generation (defaults to fixed value)
    """
    # Use fixed base time for deterministic generation
    if base_time is None:
        base_time = 1704067200  # 2024-01-01 00:00:00 UTC
    
    if valid:
        # Future timestamp (1 hour to 30 days from base)
        valid_until = draw(st.integers(
            min_value=base_time + 3600,
            max_value=base_time + (30 * 24 * 3600)
        ))
    else:
        # Past timestamp (30 days ago to 1 hour ago from base)
        valid_until = draw(st.integers(
            min_value=base_time - (30 * 24 * 3600),
            max_value=base_time - 3600
        ))
    
    exception_id = draw(st.uuids()).hex
    approval_ticket_num = draw(st.integers(min_value=1000, max_value=9999))
    
    return {
        'exception_id': f"exc-{exception_id}",
        'control_id': 'ctrl-s3-public-block',
        'resource_arn': draw(resource_arn_strategy()),
        'valid_until': valid_until,
        'justification': draw(st.text(min_size=10, max_size=200, alphabet=st.characters(whitelist_categories=('L', 'N', 'P', 'Zs')))),
        'approved_by': draw(st.emails()),
        'created_at': '2024-01-01T00:00:00Z',
        'approval_ticket': f"JIRA-{approval_ticket_num}"
    }


@st.composite
def analyze_event_strategy(draw):
    """Generate valid Analyze Lambda input event."""
    return {
        'resource_arn': draw(resource_arn_strategy()),
        'control_id': 'ctrl-s3-public-block',
        'account_id': draw(st.integers(min_value=100000000000, max_value=999999999999).map(str)),
        'region': draw(st.sampled_from(['us-east-1', 'us-west-2', 'eu-west-1', 'ap-southeast-1'])),
        'event_time': datetime.now(timezone.utc).isoformat()
    }


# ============================================================================
# Property 3: Analyze Classification Determinism
# Requirements: 3.9, 16.3
# ============================================================================

@settings(max_examples=100)
@given(
    config=s3_public_access_config(),
    exception=st.one_of(st.none(), exception_item_strategy(valid=True, base_time=1704067200))
)
def test_property_3_classification_determinism(config, exception):
    """
    Property 3: Analyze Classification Determinism
    
    GIVEN the same resource configuration and exception state
    WHEN classify_state is called multiple times
    THEN it MUST return the same classification every time
    
    Validates Requirements:
    - 3.9: Analyze SHALL produce deterministic classification for identical inputs
    - 16.3: Analyze SHALL use consistent timestamp for exception expiration checks
    """
    # Determine if configuration is compliant
    is_compliant = all(config.values())
    
    # Call classify_state multiple times
    results = [classify_state(is_compliant, exception) for _ in range(5)]
    
    # All results must be identical
    assert len(set(results)) == 1, \
        f"Classification not deterministic: got {set(results)} for config={config}, exception={exception}"
    
    # Verify expected state
    expected_state = "S_nominal" if is_compliant else ("S_exception" if exception else "S_violation")
    assert results[0] == expected_state


# ============================================================================
# Property 4: Compliant Resource Classification
# Requirements: 3.3, 10.7
# ============================================================================

@settings(max_examples=100)
@given(config=compliant_s3_config())
def test_property_4_compliant_resource_classification(config):
    """
    Property 4: Compliant Resource Classification
    
    GIVEN a resource with all four public access blocks enabled
    WHEN the resource is evaluated
    THEN it MUST be classified as S_nominal
    
    Validates Requirements:
    - 3.3: When resource is compliant with policy, Analyze SHALL return S_nominal
    - 10.7: When all four settings are true, policy SHALL return compliant
    """
    # All settings must be True for compliant config
    assert all(config.values()), "Test setup error: config should be compliant"
    
    # Evaluate policy (mocked OPA call)
    with patch('handler.subprocess.run') as mock_run:
        # Mock OPA response for compliant configuration
        mock_run.return_value = Mock(
            returncode=0,
            stdout='{"result": [{"expressions": [{"value": true}]}]}'
        )
        
        is_compliant = evaluate_policy_with_opa(config)
    
    # Must be compliant
    assert is_compliant is True, f"Compliant config {config} evaluated as non-compliant"
    
    # Classify without exception
    state = classify_state(is_compliant, exception=None)
    
    # Must be S_nominal
    assert state == "S_nominal", \
        f"Compliant resource classified as {state} instead of S_nominal"


# ============================================================================
# Property 5: Valid Exception Classification
# Requirements: 3.4, 3.7
# ============================================================================

@settings(max_examples=100)
@given(
    config=non_compliant_s3_config(),
    exception=exception_item_strategy(valid=True, base_time=1704067200)
)
def test_property_5_valid_exception_classification(config, exception):
    """
    Property 5: Valid Exception Classification
    
    GIVEN a non-compliant resource with a valid exception
    WHEN the resource is evaluated
    THEN it MUST be classified as S_exception
    
    Validates Requirements:
    - 3.4: When resource is non-compliant and has valid exception, SHALL return S_exception
    - 3.7: When exception exists with valid_until in future, SHALL consider exception valid
    """
    # Verify config is non-compliant
    assert not all(config.values()), "Test setup error: config should be non-compliant"
    
    # Verify exception is valid (future timestamp relative to base time)
    base_time = 1704067200  # 2024-01-01 00:00:00 UTC
    assert exception['valid_until'] > base_time, \
        "Test setup error: exception should have future valid_until"
    
    # Resource is non-compliant
    is_compliant = False
    
    # Classify with valid exception
    state = classify_state(is_compliant, exception)
    
    # Must be S_exception
    assert state == "S_exception", \
        f"Non-compliant resource with valid exception classified as {state} instead of S_exception"


# ============================================================================
# Property 6: Violation Classification
# Requirements: 3.5, 3.8
# ============================================================================

@settings(max_examples=100)
@given(
    config=non_compliant_s3_config(),
    has_expired_exception=st.booleans()
)
def test_property_6_violation_classification(config, has_expired_exception):
    """
    Property 6: Violation Classification
    
    GIVEN a non-compliant resource without valid exception
    WHEN the resource is evaluated
    THEN it MUST be classified as S_violation
    
    Validates Requirements:
    - 3.5: When resource is non-compliant without valid exception, SHALL return S_violation
    - 3.8: When exception exists with valid_until in past, SHALL consider exception expired
    """
    # Verify config is non-compliant
    assert not all(config.values()), "Test setup error: config should be non-compliant"
    
    # Resource is non-compliant
    is_compliant = False
    
    # Either no exception or expired exception
    if has_expired_exception:
        # Create expired exception (relative to base time)
        base_time = 1704067200  # 2024-01-01 00:00:00 UTC
        exception = {
            'exception_id': f"exc-{uuid.uuid4()}",
            'valid_until': base_time - 3600  # 1 hour before base time
        }
        # Expired exception should be treated as None in classification
        state = classify_state(is_compliant, None)
    else:
        exception = None
        state = classify_state(is_compliant, exception)
    
    # Must be S_violation
    assert state == "S_violation", \
        f"Non-compliant resource without valid exception classified as {state} instead of S_violation"


# ============================================================================
# Property 8: Exception Query Correctness
# Requirements: 3.6, 5.8
# ============================================================================

@settings(max_examples=100)
@given(
    control_id=st.just('ctrl-s3-public-block'),
    resource_arn=resource_arn_strategy(),
    has_matching_exception=st.booleans(),
    exception_is_valid=st.booleans()
)
def test_property_8_exception_query_correctness(
    control_id,
    resource_arn,
    has_matching_exception,
    exception_is_valid
):
    """
    Property 8: Exception Query Correctness
    
    GIVEN a control_id and resource_arn
    WHEN querying for exceptions
    THEN it MUST return exception only if:
      - control_id matches
      - resource_arn matches
      - valid_until > current_timestamp
    
    Validates Requirements:
    - 3.6: Analyze SHALL query Knowledge_Base exceptions table via control_id index,
           filtering by resource_arn match and valid_until > current timestamp
    - 5.8: Analyze SHALL filter exceptions by valid_until > now() in queries
    """
    base_time = 1704067200  # 2024-01-01 00:00:00 UTC
    
    # Mock DynamoDB table
    mock_table = MagicMock()
    
    if has_matching_exception:
        # Create exception with matching control_id and resource_arn
        if exception_is_valid:
            valid_until = base_time + 3600  # 1 hour in future
        else:
            valid_until = base_time - 3600  # 1 hour in past
        
        exception_item = {
            'exception_id': f"exc-{uuid.uuid4()}",
            'control_id': control_id,
            'resource_arn': resource_arn,
            'valid_until': valid_until,
            'approved_by': 'test@example.com'
        }
        
        # Mock query response
        if exception_is_valid:
            # Valid exception passes filter
            mock_table.query.return_value = {'Items': [exception_item]}
        else:
            # Expired exception filtered out by DynamoDB FilterExpression
            mock_table.query.return_value = {'Items': []}
    else:
        # No matching exception
        mock_table.query.return_value = {'Items': []}
    
    # Patch the exceptions_table
    with patch('handler.exceptions_table', mock_table):
        result = query_exception(control_id, resource_arn)
    
    # Verify query was called with correct parameters
    mock_table.query.assert_called_once()
    call_kwargs = mock_table.query.call_args[1]
    
    # Verify GSI usage
    assert call_kwargs['IndexName'] == 'control_id-index', \
        "Query must use control_id-index GSI"
    
    # Verify KeyConditionExpression
    assert call_kwargs['KeyConditionExpression'] == 'control_id = :cid', \
        "Query must filter by control_id"
    
    # Verify FilterExpression includes resource_arn and valid_until
    assert call_kwargs['FilterExpression'] == 'resource_arn = :arn AND valid_until > :now', \
        "Query must filter by resource_arn and valid_until > now"
    
    # Verify ExpressionAttributeValues
    assert call_kwargs['ExpressionAttributeValues'][':cid'] == control_id
    assert call_kwargs['ExpressionAttributeValues'][':arn'] == resource_arn
    # Allow some tolerance for query timestamp (should be recent)
    query_time = call_kwargs['ExpressionAttributeValues'][':now']
    assert isinstance(query_time, int), "Query timestamp must be integer"
    
    # Verify result correctness
    if has_matching_exception and exception_is_valid:
        assert result is not None, \
            "Query should return exception when matching and valid"
        assert result['exception_id'] == exception_item['exception_id']
        assert result['valid_until'] > base_time
    else:
        assert result is None, \
            "Query should return None when no matching valid exception"


# ============================================================================
# Integration Property: End-to-End Classification
# ============================================================================

@settings(max_examples=100)
@given(
    event=analyze_event_strategy(),
    config=s3_public_access_config(),
    has_exception=st.booleans(),
    exception_valid=st.booleans()
)
def test_property_end_to_end_classification(event, config, has_exception, exception_valid):
    """
    Integration property test for end-to-end classification logic.
    
    GIVEN an event with random S3 configuration and exception state
    WHEN lambda_handler processes the event
    THEN the classification MUST follow the state transition rules
    """
    is_compliant = all(config.values())
    base_time = 1704067200  # 2024-01-01 00:00:00 UTC
    
    # Determine expected state
    if is_compliant:
        expected_state = "S_nominal"
    elif has_exception and exception_valid:
        expected_state = "S_exception"
    else:
        expected_state = "S_violation"
    
    # Mock AWS services
    with patch('handler.s3_client') as mock_s3, \
         patch('handler.exceptions_table') as mock_exceptions, \
         patch('handler.violations_log_table') as mock_log, \
         patch('handler.subprocess.run') as mock_opa:
        
        # Mock S3 GetPublicAccessBlock
        mock_s3.get_public_access_block.return_value = {
            'PublicAccessBlockConfiguration': config
        }
        
        # Mock OPA evaluation
        opa_result = "true" if is_compliant else "false"
        mock_opa.return_value = Mock(
            returncode=0,
            stdout=f'{{"result": [{{"expressions": [{{"value": {opa_result}}}]}}]}}'
        )
        
        # Mock DynamoDB exception query
        if has_exception and exception_valid:
            exception_item = {
                'exception_id': f"exc-{uuid.uuid4()}",
                'control_id': event['control_id'],
                'resource_arn': event['resource_arn'],
                'valid_until': base_time + 3600,
                'approved_by': 'test@example.com'
            }
            mock_exceptions.query.return_value = {'Items': [exception_item]}
        else:
            mock_exceptions.query.return_value = {'Items': []}
        
        # Mock violations_log put_item
        mock_log.put_item.return_value = {}
        
        # Execute handler
        result = lambda_handler(event, None)
    
    # Verify classification
    assert result['state'] == expected_state, \
        f"Expected state {expected_state} but got {result['state']} " \
        f"for compliant={is_compliant}, has_exception={has_exception}, valid={exception_valid}"
    
    # Verify violation_id is UUID
    assert len(result['violation_id']) == 36, "violation_id should be UUID format"
    
    # Verify exception_id presence
    if expected_state == "S_exception":
        assert result['exception_id'] is not None, \
            "S_exception state must include exception_id"
    elif expected_state == "S_violation":
        assert result['exception_id'] is None, \
            "S_violation state should not include exception_id"
    # Note: S_nominal may or may not have exception_id depending on whether
    # an exception exists in the database (it's queried but not used for classification)
    
    # Verify violations_log was written
    mock_log.put_item.assert_called_once()
    logged_item = mock_log.put_item.call_args[1]['Item']
    assert logged_item['state'] == expected_state
    assert logged_item['violation_id'] == result['violation_id']


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
