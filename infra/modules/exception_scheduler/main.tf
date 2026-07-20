variable "project_name" { type = string }
variable "environment" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "exceptions_table_name" { type = string }
variable "exceptions_table_arn" { type = string }
variable "exceptions_stream_arn" { type = string }
variable "state_machine_arn" { type = string }

# --- Scheduler Group ---
resource "aws_scheduler_schedule_group" "compliance" {
  name = "compliance"
  tags = { Name = "${var.project_name}-scheduler-group", Environment = var.environment }
}

# --- Scheduler IAM (Scheduler → Step Functions) ---
resource "aws_iam_role" "compliance_scheduler" {
  name = "${var.project_name}-scheduler-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${var.project_name}-scheduler", Environment = var.environment }
}

resource "aws_iam_role_policy" "scheduler_sfn" {
  name   = "start-step-functions"
  role   = aws_iam_role.compliance_scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "states:StartExecution", Resource = var.state_machine_arn }]
  })
}

# --- Lambda ---
data "archive_file" "handler" {
  type        = "zip"
  source_file = "${path.module}/../../../lambdas/exception_scheduler_sync/handler.py"
  output_path = "${path.module}/../../../lambdas/exception_scheduler_sync/handler.zip"
}

resource "aws_lambda_function" "exception_scheduler_sync" {
  function_name    = "${var.project_name}-exception-scheduler-sync-${var.environment}"
  role             = aws_iam_role.lambda.arn
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  handler          = "handler.handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      EXCEPTIONS_TABLE   = var.exceptions_table_name
      STATE_MACHINE_ARN  = var.state_machine_arn
      SCHEDULER_ROLE_ARN = aws_iam_role.compliance_scheduler.arn
      SCHEDULE_GROUP     = aws_scheduler_schedule_group.compliance.name
      ACCOUNT_ID         = var.account_id
    }
  }

  tags = { Name = "${var.project_name}-exception-scheduler-sync", Environment = var.environment }
}

# --- Event Source Mapping ---
resource "aws_lambda_event_source_mapping" "exceptions_stream" {
  event_source_arn                       = var.exceptions_stream_arn
  function_name                          = aws_lambda_function.exception_scheduler_sync.arn
  starting_position                      = "LATEST"
  batch_size                             = 10
  bisect_batch_on_function_error         = true
  maximum_retry_attempts                 = 3

  # Honor the handler's {"batchItemFailures": [...]} return so a single failed
  # record is retried/checkpointed instead of being silently dropped with the
  # rest of the batch.
  function_response_types = ["ReportBatchItemFailures"]

  filter_criteria {
    filter {
      pattern = jsonencode({ eventName = ["INSERT", "MODIFY"] })
    }
  }

  depends_on = [aws_iam_role_policy.lambda]
}

# --- Lambda IAM ---
resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-exception-scheduler-sync-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${var.project_name}-exception-scheduler-sync", Environment = var.environment }
}

resource "aws_iam_role_policy" "lambda" {
  name = "exception-scheduler-sync"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:GetItem"], Resource = var.exceptions_table_arn },
      { Effect = "Allow", Action = ["scheduler:CreateSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule"],
        Resource = "arn:aws:scheduler:${var.region}:${var.account_id}:schedule/${aws_scheduler_schedule_group.compliance.name}/*" },
      { Effect = "Allow", Action = "iam:PassRole", Resource = aws_iam_role.compliance_scheduler.arn },
      { Effect = "Allow", Action = "states:StartExecution", Resource = var.state_machine_arn },
      { Effect = "Allow", Action = ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"],
        Resource = "${var.exceptions_table_arn}/stream/*" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" }
    ]
  })
}
