variable "project_name" { type = string }
variable "environment" { type = string }
variable "ecr_repository_url" { type = string }

resource "aws_lambda_function" "execute" {
  function_name = "${var.project_name}-execute-${var.environment}"
  role          = aws_iam_role.execute_lambda.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:latest"
  architectures = ["arm64"]
  timeout       = 90
  memory_size   = 2048

  ephemeral_storage { size = 1024 }

  environment {
    variables = { TOFU_PLUGIN_DIR = "/opt/providers" }
  }

  tags = { Name = "${var.project_name}-execute", Environment = var.environment }

  lifecycle { ignore_changes = [image_uri] }
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
    Statement = [{ Effect = "Allow", Action = ["s3:PutBucketPublicAccessBlock", "s3:GetBucketPublicAccessBlock", "s3:PutBucketPolicy", "s3:GetBucketPolicy"], Resource = "arn:aws:s3:::*" }]
  })
}

output "function_arn" { value = aws_lambda_function.execute.arn }
output "function_name" { value = aws_lambda_function.execute.function_name }
output "role_name" { value = aws_iam_role.execute_lambda.name }
