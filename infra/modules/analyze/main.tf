variable "project_name" { type = string }
variable "environment" { type = string }
variable "exceptions_table_name" { type = string }
variable "exceptions_table_arn" { type = string }
variable "policy_bucket" { type = string }
variable "opa_layer_arn" { type = string }
variable "account_id" { type = string }
variable "log_retention_days" {
  type    = number
  default = 30
}

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
  # 60s, not 15: worst case is (n_controls x OPA_MAX_TIMEOUT). CreateBucket
  # alone fans out to 4 S3 controls, so 15s could not even cover the nominal
  # worst case, and a timeout here loses the per-control rows for ALL controls.
  # The handler additionally derives a per-control budget from the time left.
  timeout     = 60
  memory_size = 512
  layers      = [var.opa_layer_arn]

  environment {
    variables = {
      EXCEPTIONS_TABLE = var.exceptions_table_name
      POLICY_BUCKET    = var.policy_bucket
    }
  }

  tags = { Name = "${var.project_name}-analyze", Environment = var.environment }

  depends_on = [aws_cloudwatch_log_group.lambda]
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
        Sid    = "S3BucketRead"
        Effect = "Allow"
        # NOTE: the IAM action for the GetBucketEncryption API is
        # s3:GetEncryptionConfiguration — "s3:GetBucketEncryption" does not
        # exist and grants nothing (every ctrl-s3-encryption eval failed
        # AccessDenied until this was fixed).
        Action   = ["s3:GetBucketPublicAccessBlock", "s3:GetBucketPolicy", "s3:GetEncryptionConfiguration", "s3:GetBucketVersioning", "s3:GetBucketLogging"]
        Resource = "arn:aws:s3:::*"
        # A control is data in a table and names the resource to read; this keeps
        # a tampered one from reaching a bucket in another account. Not applied to
        # the EC2 Describe* calls: those carry no resource in the request context,
        # so the condition would never match and would deny silently.
        Condition = { StringEquals = { "aws:ResourceAccount" = var.account_id } }
      },
      # EC2 — the Describe* APIs accept neither resource ARNs nor tag
      # conditions: a statement scoped by aws:ResourceTag never matches and
      # denies silently. ctrl-ebs-volume-encrypted failed exactly that way
      # (UnauthorizedOperation on DescribeVolumes) until these actions moved
      # out of the ABAC statement. They are read-only and answer for every
      # resource of their type; scoping them is a matter for the policy that
      # evaluates the answer, not for IAM.
      {
        Sid    = "EC2DescribeNoResourceScope"
        Effect = "Allow"
        Action = ["ec2:DescribeSecurityGroupRules", "ec2:DescribeSecurityGroups", "ec2:DescribeVolumes",
        "ec2:GetEbsEncryptionByDefault"]
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

# Declared so its retention is set: the group Lambda creates on its own keeps
# logs forever. Named literally (not from the function) so the function can
# depend on it and never create an unmanaged one first. Imported once from the
# group Lambda had already created (infra/imports.tf).
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}-analyze-${var.environment}"
  retention_in_days = var.log_retention_days
  tags              = { Name = "${var.project_name}-analyze-logs", Environment = var.environment }
}

output "log_group_name" { value = aws_cloudwatch_log_group.lambda.name }
