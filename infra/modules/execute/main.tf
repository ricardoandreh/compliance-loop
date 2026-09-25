variable "project_name" { type = string }
variable "environment" { type = string }
variable "tofu_layer_arn" { type = string }
variable "remediation_bucket" { type = string }
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "account_id" { type = string }
variable "protected_bucket_arns" {
  description = "Buckets the loop must never be able to rewrite — its own evidence (CloudTrail) and the state it is deployed from."
  type        = list(string)
}

data "archive_file" "handler" {
  type        = "zip"
  source_file = "${path.module}/../../../lambdas/execute/handler.py"
  output_path = "${path.module}/../../../lambdas/execute/execute.zip"
}

resource "aws_lambda_function" "execute" {
  function_name    = "${var.project_name}-execute-${var.environment}"
  role             = aws_iam_role.execute_lambda.arn
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  timeout          = 90
  memory_size      = 2048
  layers           = [var.tofu_layer_arn]

  ephemeral_storage { size = 1024 }

  environment {
    variables = {
      REMEDIATION_BUCKET   = var.remediation_bucket
      PROVIDER_CACHE_KEY   = "provider-cache/tf-dot-terraform.tar.gz"
      REMEDIATION_ROLE_ARN = aws_iam_role.remediation.arn
    }
  }

  tags = { Name = "${var.project_name}-execute", Environment = var.environment }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_iam_role" "execute_lambda" {
  name = "${var.project_name}-execute-lambda-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${var.project_name}-execute-lambda", Environment = var.environment }
}

resource "aws_iam_role_policy" "logging" {
  name = "cloudwatch-logging"
  role = aws_iam_role.execute_lambda.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" }]
  })
}

# The Lambda itself can no longer write to any resource. It downloads its own
# artefacts and assumes the remediation role, once per violation, with a session
# policy naming the resource that was classified — so the authority to change a
# resource is derived from the evaluation, not held permanently.
resource "aws_iam_role_policy" "execute_own" {
  name = "artefacts-and-assume"
  role = aws_iam_role.execute_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = [
        "arn:aws:s3:::${var.remediation_bucket}/controls/*/remediation.tf",
        "arn:aws:s3:::${var.remediation_bucket}/provider-cache/*"
      ] },
      { Effect = "Allow", Action = ["sts:AssumeRole"], Resource = aws_iam_role.remediation.arn }
    ]
  })
}

# --- Remediation role: the widest the loop can ever be ---
#
# A session policy can only narrow this, never widen it, so this is the ceiling:
# the eight configuration APIs the controls need, inside this account only, and
# never over the buckets that hold the loop's own evidence or state.
resource "aws_iam_role" "remediation" {
  name = "${var.project_name}-remediation-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow",
      Principal = { AWS = aws_iam_role.execute_lambda.arn },
      Action    = "sts:AssumeRole"
    }]
  })
  max_session_duration = 3600
  tags                 = { Name = "${var.project_name}-remediation", Environment = var.environment }
}

resource "aws_iam_role_policy" "remediation" {
  name = "resource-remediation"
  role = aws_iam_role.remediation.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "S3ConfigurationWrite", Effect = "Allow", Action = [
        "s3:PutBucketPublicAccessBlock", "s3:GetBucketPublicAccessBlock",
        "s3:PutBucketPolicy", "s3:GetBucketPolicy",
        "s3:PutBucketVersioning", "s3:GetBucketVersioning",
        "s3:PutEncryptionConfiguration", "s3:GetEncryptionConfiguration"
        ], Resource = "arn:aws:s3:::*",
      Condition = { StringEquals = { "aws:ResourceAccount" = var.account_id } } },
      { Sid = "NeverRewriteOwnEvidence", Effect = "Deny", Action = "s3:*",
      Resource = concat(var.protected_bucket_arns, [for arn in var.protected_bucket_arns : "${arn}/*"]) },
      { Sid = "EbsAccountSetting", Effect = "Allow",
      Action = ["ec2:EnableEbsEncryptionByDefault", "ec2:GetEbsEncryptionByDefault"], Resource = "*" }
    ]
  })
}

output "function_arn" { value = aws_lambda_function.execute.arn }
output "function_name" { value = aws_lambda_function.execute.function_name }
output "role_name" { value = aws_iam_role.execute_lambda.name }
output "role_arn" { value = aws_iam_role.execute_lambda.arn }
# The identity that appears in CloudTrail for every remediation write — what the
# EventBridge rule filters out to keep the loop from reacting to itself.
output "remediation_role_arn" { value = aws_iam_role.remediation.arn }

# Declared so its retention is set: the group Lambda creates on its own keeps
# logs forever. Named literally (not from the function) so the function can
# depend on it and never create an unmanaged one first. Imported once from the
# group Lambda had already created (infra/imports.tf).
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}-execute-${var.environment}"
  retention_in_days = var.log_retention_days
  tags              = { Name = "${var.project_name}-execute-logs", Environment = var.environment }
}
