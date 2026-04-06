# DynamoDB table for violations log with GSI
# Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10, 6.11
resource "aws_dynamodb_table" "violations_log" {
  name         = "${var.project_name}-violations-log-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "violation_id"

  attribute {
    name = "violation_id"
    type = "S"
  }

  attribute {
    name = "resource_arn"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  # Global Secondary Index for querying violations by resource and time
  global_secondary_index {
    name            = "resource_arn-timestamp-index"
    hash_key        = "resource_arn"
    range_key       = "timestamp"
    projection_type = "ALL"
  }

  tags = {
    Name        = "${var.project_name}-violations-log-${var.environment}"
    Environment = var.environment
    ManagedBy   = "OpenTofu"
  }
}
