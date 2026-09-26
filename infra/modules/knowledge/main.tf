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

  stream_enabled   = true
  stream_view_type = "KEYS_ONLY"

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
    name = "resource_arn"
    type = "S"
  }

  attribute {
    name = "control_id"
    type = "S"
  }

  # Targeted lookup: query the exception(s) for a specific (resource, control)
  # pair instead of scanning every exception of a control and filtering by
  # resource_arn afterwards. hash_key=resource_arn + range_key=control_id makes
  # query_exception a point query (reads only the 1-N items for the pair),
  # eliminating RCU amplification on popular controls. exception_id remains the
  # table PK, so each grant keeps its own immutable audit record (H2) and the
  # scheduler-sync (keyed by exception_id) is unaffected.
  global_secondary_index {
    name = "resource-control-index"
    key_schema {
      attribute_name = "resource_arn"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "control_id"
      key_type       = "RANGE"
    }
    projection_type    = "INCLUDE"
    non_key_attributes = ["valid_until", "exception_id", "approved_by"]
  }

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  ttl {
    attribute_name = "valid_until"
    enabled        = true
  }

  # Who approved which exception, and until when: audit data too.
  point_in_time_recovery { enabled = true }

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
    name = "resource_arn-timestamp-index"
    key_schema {
      attribute_name = "resource_arn"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "timestamp"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
  }

  # The audit trail (H2) is the one table that cannot be rebuilt from anything
  # else: continuous backups, restorable to any second of the last 35 days.
  point_in_time_recovery { enabled = true }

  tags = { Name = "${var.project_name}-violations-log-${var.environment}", Environment = var.environment, ManagedBy = "OpenTofu" }
}

output "controls_table_name" { value = aws_dynamodb_table.controls.name }
output "controls_table_arn" { value = aws_dynamodb_table.controls.arn }
output "controls_stream_arn" { value = aws_dynamodb_table.controls.stream_arn }
output "exceptions_table_name" { value = aws_dynamodb_table.exceptions.name }
output "exceptions_table_arn" { value = aws_dynamodb_table.exceptions.arn }
output "exceptions_stream_arn" { value = aws_dynamodb_table.exceptions.stream_arn }
output "violations_log_table_name" { value = aws_dynamodb_table.violations_log.name }
output "violations_log_table_arn" { value = aws_dynamodb_table.violations_log.arn }
