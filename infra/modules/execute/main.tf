variable "project_name" { type = string }
variable "environment" { type = string }
variable "tofu_layer_arn" { type = string }
variable "remediation_bucket" { type = string }

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
      REMEDIATION_BUCKET = var.remediation_bucket
      PROVIDER_CACHE_KEY = "provider-cache/tf-dot-terraform.tar.gz"
    }
  }

  tags = { Name = "${var.project_name}-execute", Environment = var.environment }
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

resource "aws_iam_role_policy" "s3" {
  name = "s3-remediation"
  role = aws_iam_role.execute_lambda.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = [
        "s3:PutBucketPublicAccessBlock", "s3:GetBucketPublicAccessBlock",
        "s3:PutBucketPolicy", "s3:GetBucketPolicy",
        "s3:PutBucketVersioning", "s3:GetBucketVersioning",
        "s3:PutEncryptionConfiguration", "s3:GetEncryptionConfiguration"
      ], Resource = "arn:aws:s3:::*" },
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = [
        "arn:aws:s3:::${var.remediation_bucket}/controls/*/remediation.tf",
        "arn:aws:s3:::${var.remediation_bucket}/provider-cache/*"
      ]},
      { Effect = "Allow", Action = ["ec2:EnableEbsEncryptionByDefault", "ec2:GetEbsEncryptionByDefault"], Resource = "*" }
    ]
  })
}

output "function_arn" { value = aws_lambda_function.execute.arn }
output "function_name" { value = aws_lambda_function.execute.function_name }
output "role_name" { value = aws_iam_role.execute_lambda.name }
