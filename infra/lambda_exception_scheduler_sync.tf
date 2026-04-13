# Exception Scheduler Sync: DynamoDB Streams → Lambda → EventBridge Scheduler
# Replaces the EventBridge Pipe approach (non-deterministic TTL latency)
# with precise one-time schedules per exception.

# --- Scheduler Group ---
resource "aws_scheduler_schedule_group" "compliance" {
  name = "compliance"

  tags = {
    Name        = "${var.project_name}-scheduler-group"
    Environment = var.environment
  }
}

# --- IAM Role: Scheduler → Step Functions ---
resource "aws_iam_role" "compliance_scheduler" {
  name = "${var.project_name}-scheduler-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name        = "${var.project_name}-scheduler"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy" "compliance_scheduler" {
  name = "start-step-functions"
  role = aws_iam_role.compliance_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = aws_sfn_state_machine.compliance_loop.arn
    }]
  })
}

# --- Lambda: exception-scheduler-sync ---
data "archive_file" "exception_scheduler_sync" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/exception_scheduler_sync/handler.py"
  output_path = "${path.module}/../lambdas/exception_scheduler_sync/handler.zip"
}

resource "aws_lambda_function" "exception_scheduler_sync" {
  function_name = "${var.project_name}-exception-scheduler-sync-${var.environment}"
  role          = aws_iam_role.exception_scheduler_sync.arn

  filename         = data.archive_file.exception_scheduler_sync.output_path
  source_code_hash = data.archive_file.exception_scheduler_sync.output_base64sha256
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      EXCEPTIONS_TABLE  = aws_dynamodb_table.exceptions.name
      STATE_MACHINE_ARN = aws_sfn_state_machine.compliance_loop.arn
      SCHEDULER_ROLE_ARN = aws_iam_role.compliance_scheduler.arn
      SCHEDULE_GROUP    = aws_scheduler_schedule_group.compliance.name
      ACCOUNT_ID        = data.aws_caller_identity.current.account_id
    }
  }

  tags = {
    Name        = "${var.project_name}-exception-scheduler-sync"
    Environment = var.environment
    Purpose     = "Sync DynamoDB exceptions to EventBridge Scheduler"
  }
}

# --- Event Source Mapping: DynamoDB Stream → Lambda ---
resource "aws_lambda_event_source_mapping" "exceptions_stream" {
  event_source_arn       = aws_dynamodb_table.exceptions.stream_arn
  function_name          = aws_lambda_function.exception_scheduler_sync.arn
  starting_position      = "LATEST"
  batch_size             = 10
  bisect_batch_on_function_error = true
  maximum_retry_attempts = 3

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT", "MODIFY"]
      })
    }
  }

  depends_on = [aws_iam_role_policy.exception_scheduler_sync]
}

# --- IAM Role: Lambda ---
resource "aws_iam_role" "exception_scheduler_sync" {
  name = "${var.project_name}-exception-scheduler-sync-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name        = "${var.project_name}-exception-scheduler-sync"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy" "exception_scheduler_sync" {
  name = "exception-scheduler-sync"
  role = aws_iam_role.exception_scheduler_sync.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = aws_dynamodb_table.exceptions.arn
      },
      {
        Effect = "Allow"
        Action = [
          "scheduler:CreateSchedule",
          "scheduler:UpdateSchedule",
          "scheduler:DeleteSchedule",
          "scheduler:GetSchedule"
        ]
        Resource = "arn:aws:scheduler:${var.region}:${data.aws_caller_identity.current.account_id}:schedule/${aws_scheduler_schedule_group.compliance.name}/*"
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.compliance_scheduler.arn
      },
      {
        Effect   = "Allow"
        Action   = "states:StartExecution"
        Resource = aws_sfn_state_machine.compliance_loop.arn
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:DescribeStream",
          "dynamodb:ListStreams"
        ]
        Resource = "${aws_dynamodb_table.exceptions.arn}/stream/*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}
