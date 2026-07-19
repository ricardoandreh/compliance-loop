variable "project_name" { type = string }
variable "environment" { type = string }
variable "exceptions_table_name" { type = string }
variable "exceptions_table_arn" { type = string }
variable "policy_bucket" { type = string }
variable "opa_layer_arn" { type = string }

data "archive_file" "handler" {
  type        = "zip"
  source_file = "${path.module}/../../../lambdas/analyze/handler.py"
  output_path = "${path.module}/../../../lambdas/analyze/analyze.zip"
}

resource "aws_lambda_function" "analyze" {
  function_name    = "${var.project_name}-analyze-${var.environment}"
  role             = aws_iam_role.analyze_lambda.arn
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  timeout          = 15
  memory_size      = 512
  layers           = [var.opa_layer_arn]

  environment {
    variables = {
      EXCEPTIONS_TABLE = var.exceptions_table_name
      POLICY_BUCKET    = var.policy_bucket
    }
  }

  tags = { Name = "${var.project_name}-analyze", Environment = var.environment }
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

resource "aws_iam_role_policy" "resource_read" {
  name = "resource-read-abac"
  role = aws_iam_role.analyze_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 — bucket-level read (tags not supported on bucket APIs)
      {
        Sid      = "S3BucketRead"
        Effect   = "Allow"
        Action   = ["s3:GetBucketPublicAccessBlock", "s3:GetBucketPolicy", "s3:GetBucketEncryption", "s3:GetBucketVersioning", "s3:GetBucketLogging"]
        Resource = "arn:aws:s3:::*"
      },
      # EC2 — describe with ABAC tag condition
      {
        Sid       = "EC2DescribeABAC"
        Effect    = "Allow"
        Action    = ["ec2:DescribeSecurityGroupRules", "ec2:DescribeSecurityGroups", "ec2:GetEbsEncryptionByDefault"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:ResourceTag/compliance-managed" = "true" } }
      },
      # EC2 — describe calls that don't support ABAC (account-level)
      {
        Sid      = "EC2AccountLevel"
        Effect   = "Allow"
        Action   = ["ec2:GetEbsEncryptionByDefault"]
        Resource = "*"
      },
      # RDS — describe with ABAC tag condition
      {
        Sid       = "RDSDescribeABAC"
        Effect    = "Allow"
        Action    = ["rds:DescribeDBInstances"]
        Resource  = "arn:aws:rds:*:*:db:*"
        Condition = { StringEquals = { "aws:ResourceTag/compliance-managed" = "true" } }
      },
      # CloudTrail — read with ABAC tag condition
      {
        Sid       = "CloudTrailReadABAC"
        Effect    = "Allow"
        Action    = ["cloudtrail:GetTrailStatus", "cloudtrail:DescribeTrails"]
        Resource  = "arn:aws:cloudtrail:*:*:trail/*"
        Condition = { StringEquals = { "aws:ResourceTag/compliance-managed" = "true" } }
      },
      # IAM — account-level (no ABAC possible)
      {
        Sid      = "IAMAccountRead"
        Effect   = "Allow"
        Action   = ["iam:GetAccountPasswordPolicy"]
        Resource = "*"
      },
      # S3 policy bucket — Rego policies
      {
        Sid      = "S3PolicyBucket"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.policy_bucket}/controls/*/policy.rego"
      }
    ]
  })
}

resource "aws_iam_role_policy" "dynamodb" {
  name = "dynamodb-access"
  role = aws_iam_role.analyze_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:Query"], Resource = [var.exceptions_table_arn, "${var.exceptions_table_arn}/index/*"] }
    ]
  })
}

output "function_arn" { value = aws_lambda_function.analyze.arn }
output "function_name" { value = aws_lambda_function.analyze.function_name }
