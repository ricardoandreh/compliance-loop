variable "project_name" { type = string }
variable "environment" { type = string }
variable "controls_table_name" { type = string }
variable "controls_table_arn" { type = string }
variable "controls_stream_arn" { type = string }
variable "eventbridge_rule_name" { type = string }
variable "eventbridge_rule_arn" { type = string }
variable "remediation_role_arn" { type = string }
variable "stream_dlq_arn" { type = string }
variable "log_retention_days" {
  type    = number
  default = 30
}

data "archive_file" "handler" {
  type        = "zip"
  source_file = "${path.module}/../../../lambdas/controls_sync/handler.py"
  output_path = "${path.module}/../../../lambdas/controls_sync/handler.zip"
}

resource "aws_lambda_function" "controls_sync" {
  function_name    = "${var.project_name}-controls-sync-${var.environment}"
  role             = aws_iam_role.lambda.arn
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  timeout          = 15
  memory_size      = 128

  environment {
    variables = {
      CONTROLS_TABLE        = var.controls_table_name
      EVENTBRIDGE_RULE_NAME = var.eventbridge_rule_name
      REMEDIATION_ROLE_ARN  = var.remediation_role_arn
    }
  }

  tags = { Name = "${var.project_name}-controls-sync", Environment = var.environment }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_event_source_mapping" "controls_stream" {
  event_source_arn  = var.controls_stream_arn
  function_name     = aws_lambda_function.controls_sync.arn
  starting_position = "LATEST"
  batch_size        = 10

  # Without these, a record that always fails is retried until it expires
  # (24 h) and blocks the shard behind it: one bad sync would freeze the rule
  # for a day. Bounded retries, then the record goes to the stream DLQ, where an
  # alarm fires and it can be replayed.
  maximum_retry_attempts         = 3
  maximum_record_age_in_seconds  = 3600
  bisect_batch_on_function_error = true
  destination_config {
    on_failure { destination_arn = var.stream_dlq_arn }
  }
}

resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-controls-sync-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "permissions" {
  name = "permissions"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" },
      { Effect = "Allow", Action = ["dynamodb:Scan"], Resource = var.controls_table_arn },
      { Effect = "Allow", Action = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"], Resource = "${var.controls_table_arn}/stream/*" },
      { Effect = "Allow", Action = ["events:PutRule", "events:DisableRule"], Resource = var.eventbridge_rule_arn },
      # On-failure destination of the stream mapping (bounded retries, then DLQ).
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = var.stream_dlq_arn }
    ]
  })
}

# Declared so its retention is set: the group Lambda creates on its own keeps
# logs forever. Named literally (not from the function) so the function can
# depend on it and never create an unmanaged one first. Imported once from the
# group Lambda had already created (infra/imports.tf).
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}-controls-sync-${var.environment}"
  retention_in_days = var.log_retention_days
  tags              = { Name = "${var.project_name}-controls-sync-logs", Environment = var.environment }
}

output "function_name" { value = aws_lambda_function.controls_sync.function_name }
