variable "project_name" { type = string }
variable "environment" { type = string }
variable "controls_table_name" { type = string }
variable "controls_table_arn" { type = string }
variable "exceptions_table_name" { type = string }
variable "exceptions_table_arn" { type = string }
variable "policy_bucket" { type = string }

resource "aws_lambda_function" "analyze" {
  function_name    = "${var.project_name}-analyze-${var.environment}"
  role             = aws_iam_role.analyze_lambda.arn
  filename         = "${path.module}/../../../lambdas/analyze/analyze.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../lambdas/analyze/analyze.zip")
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  timeout          = 15
  memory_size      = 512

  environment {
    variables = {
      CONTROLS_TABLE   = var.controls_table_name
      EXCEPTIONS_TABLE = var.exceptions_table_name
      POLICY_BUCKET    = var.policy_bucket
    }
  }

  tags = { Name = "${var.project_name}-analyze", Environment = var.environment }

  lifecycle { ignore_changes = [filename, source_code_hash] }
}

resource "aws_iam_role" "analyze_lambda" {
  name = "${var.project_name}-analyze-lambda-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${var.project_name}-analyze-lambda", Environment = var.environment }
}

resource "aws_iam_role_policy" "logging" {
  name = "cloudwatch-logging"
  role = aws_iam_role.analyze_lambda.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" }]
  })
}

resource "aws_iam_role_policy" "s3" {
  name = "s3-access"
  role = aws_iam_role.analyze_lambda.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["s3:GetBucketPublicAccessBlock", "s3:GetBucketPolicy"], Resource = "arn:aws:s3:::*" },
                 { Effect = "Allow", Action = ["s3:GetObject"], Resource = "arn:aws:s3:::${var.policy_bucket}/controls/*/policy.rego" }]
  })
}

resource "aws_iam_role_policy" "dynamodb" {
  name = "dynamodb-access"
  role = aws_iam_role.analyze_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:GetItem"], Resource = [var.controls_table_arn] },
      { Effect = "Allow", Action = ["dynamodb:Query"], Resource = [var.exceptions_table_arn, "${var.exceptions_table_arn}/index/*"] }
    ]
  })
}

output "function_arn" { value = aws_lambda_function.analyze.arn }
output "function_name" { value = aws_lambda_function.analyze.function_name }
