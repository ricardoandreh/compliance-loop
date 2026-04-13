variable "project_name" { type = string }
variable "environment" { type = string }

resource "aws_dynamodb_table" "controls" {
  name         = "${var.project_name}-controls-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "control_id"

  attribute {
    name = "control_id"
    type = "S"
  }

  tags = { Name = "${var.project_name}-controls-${var.environment}", Environment = var.environment, ManagedBy = "OpenTofu" }
}

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

  global_secondary_index {
    name            = "control_id-index"
    hash_key        = "control_id"
    projection_type = "ALL"
  }

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  ttl {
    attribute_name = "valid_until"
    enabled        = true
  }

  tags = { Name = "${var.project_name}-exceptions-${var.environment}", Environment = var.environment, ManagedBy = "OpenTofu" }
}

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

  global_secondary_index {
    name            = "resource_arn-timestamp-index"
    hash_key        = "resource_arn"
    range_key       = "timestamp"
    projection_type = "ALL"
  }

  tags = { Name = "${var.project_name}-violations-log-${var.environment}", Environment = var.environment, ManagedBy = "OpenTofu" }
}

output "controls_table_name" { value = aws_dynamodb_table.controls.name }
output "controls_table_arn" { value = aws_dynamodb_table.controls.arn }
output "exceptions_table_name" { value = aws_dynamodb_table.exceptions.name }
output "exceptions_table_arn" { value = aws_dynamodb_table.exceptions.arn }
output "exceptions_stream_arn" { value = aws_dynamodb_table.exceptions.stream_arn }
output "violations_log_table_name" { value = aws_dynamodb_table.violations_log.name }
output "violations_log_table_arn" { value = aws_dynamodb_table.violations_log.arn }
