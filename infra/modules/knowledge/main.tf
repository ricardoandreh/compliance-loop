variable "project_name" { type = string }
variable "environment" { type = string }
variable "account_id" { type = string }

variable "exception_writer_principals" {
  description = "Principals allowed to assume the exception-writer role; empty means any principal of the account that IAM lets assume it"
  type        = list(string)
  default     = []
}

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

# --- Who may write an exception ---
#
# The loop obeys every row of the exceptions table: whoever can write a row can
# switch remediation off for a resource. Granting an exception (who may ask, who
# approves, for how long) belongs to the platform that consumes the loop, not to
# the loop (L20); what the loop can guarantee is that the platform's channel is
# the only way in. The writer role is that channel, and the table's own resource
# policy denies every write that does not come through it, whatever the caller's
# IAM policies allow.
#
# The channel attests approved_by. CloudTrail records each write as a data event
# with the session that made it, so the row and the trail can be checked against
# each other. Whoever can change this policy (dynamodb:PutResourcePolicy) or the
# role's trust is still above it: the guarantee moves to IAM administration, it
# does not disappear.
resource "aws_iam_role" "exception_writer" {
  name = "${var.project_name}-exception-writer-${var.environment}"
  # Without a list, trust the account: IAM still decides who may sts:AssumeRole.
  # The platform narrows it to its approval pipeline (e.g. the CI role of an
  # exceptions repository).
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = length(var.exception_writer_principals) > 0 ? var.exception_writer_principals : ["arn:aws:iam::${var.account_id}:root"] }
      Action    = "sts:AssumeRole"
    }]
  })
  max_session_duration = 3600
  tags                 = { Name = "${var.project_name}-exception-writer", Environment = var.environment, ManagedBy = "OpenTofu" }
}

locals {
  exception_write_actions = [
    "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem",
    "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete",
  ]
}

resource "aws_iam_role_policy" "exception_writer" {
  name = "exceptions-write"
  role = aws_iam_role.exception_writer.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = concat(local.exception_write_actions, ["dynamodb:GetItem"]), Resource = aws_dynamodb_table.exceptions.arn }]
  })
}

resource "aws_dynamodb_resource_policy" "exceptions" {
  resource_arn = aws_dynamodb_table.exceptions.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheExceptionWriterWrites"
      Effect    = "Deny"
      Principal = "*"
      Action    = local.exception_write_actions
      Resource  = aws_dynamodb_table.exceptions.arn
      # aws:PrincipalArn of an assumed-role session is the role's ARN.
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = aws_iam_role.exception_writer.arn } }
    }]
  })
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
output "exception_writer_role_arn" { value = aws_iam_role.exception_writer.arn }
output "violations_log_table_name" { value = aws_dynamodb_table.violations_log.name }
output "violations_log_table_arn" { value = aws_dynamodb_table.violations_log.arn }
