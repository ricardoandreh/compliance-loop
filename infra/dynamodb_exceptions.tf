# DynamoDB table for compliance exceptions with TTL
# Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.10, 5.11, 5.12
resource "aws_dynamodb_table" "exceptions" {
  name         = "${var.project_name}-exceptions-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "exception_id"

  attribute {
    name = "exception_id"
    type = "S"
  }

  attribute {
    name = "control_id"
    type = "S"
  }

  # Global Secondary Index for querying exceptions by control_id
  global_secondary_index {
    name            = "control_id-index"
    hash_key        = "control_id"
    projection_type = "ALL"
  }

  # Stream for exception-scheduler-sync Lambda
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  # TTL configuration for automatic expiration
  ttl {
    attribute_name = "valid_until"
    enabled        = true
  }

  tags = {
    Name        = "${var.project_name}-exceptions-${var.environment}"
    Environment = var.environment
    ManagedBy   = "OpenTofu"
  }
}
