variable "project_name" { type = string }
variable "environment" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "escalation_queue_arn" { type = string }
variable "violations_log_table_name" { type = string }
variable "violations_log_table_arn" { type = string }

data "archive_file" "handler" {
  type        = "zip"
  source_file = "${path.module}/../../../lambdas/escalation/handler.py"
  output_path = "${path.module}/../../../lambdas/escalation/escalation.zip"
}

resource "aws_lambda_function" "escalation" {
  function_name    = "${var.project_name}-escalation-${var.environment}"
  role             = aws_iam_role.escalation_lambda.arn
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  timeout          = 30
  memory_size      = 256

  environment {
    variables = { VIOLATIONS_TABLE = var.violations_log_table_name }
  }

  tags = { Name = "${var.project_name}-escalation", Environment = var.environment }
}

resource "aws_lambda_event_source_mapping" "escalation_sqs" {
  event_source_arn = var.escalation_queue_arn
  function_name    = aws_lambda_function.escalation.arn
  batch_size       = 1
  enabled          = true
  # Without this, the handler's batchItemFailures return is ignored and a
  # failed GitHub call still deletes the message — escalation silently lost.
  function_response_types = ["ReportBatchItemFailures"]
  depends_on              = [aws_iam_role_policy.sqs]
}

resource "aws_iam_role" "escalation_lambda" {
  name = "${var.project_name}-escalation-lambda-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${var.project_name}-escalation-lambda", Environment = var.environment }
}

resource "aws_iam_role_policy" "logging" {
  name = "cloudwatch-logging"
  role = aws_iam_role.escalation_lambda.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" }]
  })
}

resource "aws_iam_role_policy" "sqs" {
  name = "sqs-access"
  role = aws_iam_role.escalation_lambda.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = var.escalation_queue_arn }]
  })
}

resource "aws_iam_role_policy" "dynamodb" {
  name = "dynamodb-access"
  role = aws_iam_role.escalation_lambda.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["dynamodb:UpdateItem"], Resource = var.violations_log_table_arn }]
  })
}

resource "aws_iam_role_policy" "ssm" {
  name = "ssm-github-params"
  role = aws_iam_role.escalation_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ssm:GetParameters"], Resource = "arn:aws:ssm:${var.region}:${var.account_id}:parameter/compliance/github/*" },
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = "*", Condition = { StringEquals = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" } } }
    ]
  })
}
