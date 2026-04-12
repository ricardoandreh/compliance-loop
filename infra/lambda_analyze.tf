# Placeholder for Analyze Lambda (to be implemented in task 5)
# This resource is referenced by the Step Functions state machine

resource "aws_lambda_function" "analyze" {
  function_name = "${var.project_name}-analyze-${var.environment}"
  role          = aws_iam_role.analyze_lambda.arn
  
  filename         = "${path.module}/../lambdas/analyze/analyze.zip"
  source_code_hash = filebase64sha256("${path.module}/../lambdas/analyze/analyze.zip")
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 15
  memory_size   = 512

  environment {
    variables = {
      CONTROLS_TABLE    = aws_dynamodb_table.controls.name
      EXCEPTIONS_TABLE  = aws_dynamodb_table.exceptions.name
      VIOLATIONS_LOG_TABLE  = aws_dynamodb_table.violations_log.name
      POLICY_PACKAGE    = "s3_public_access"
    }
  }

  tags = {
    Name        = "${var.project_name}-analyze"
    Environment = var.environment
    Purpose     = "Analyze component of MAPE-K loop"
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

# IAM role for Analyze Lambda (to be expanded in task 10)
resource "aws_iam_role" "analyze_lambda" {
  name = "${var.project_name}-analyze-lambda-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-analyze-lambda"
    Environment = var.environment
  }
}

# CloudWatch Logs policy for Analyze Lambda
resource "aws_iam_role_policy" "analyze_lambda_logging" {
  name = "cloudwatch-logging"
  role = aws_iam_role.analyze_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
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

# S3 permissions for Analyze Lambda
resource "aws_iam_role_policy" "analyze_lambda_s3" {
  name = "s3-access"
  role = aws_iam_role.analyze_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetBucketPublicAccessBlock"
        ]
        Resource = "arn:aws:s3:::*"
      }
    ]
  })
}

# DynamoDB permissions for Analyze Lambda
resource "aws_iam_role_policy" "analyze_lambda_dynamodb" {
  name = "dynamodb-access"
  role = aws_iam_role.analyze_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.exceptions.arn,
          "${aws_dynamodb_table.exceptions.arn}/index/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem"
        ]
        Resource = aws_dynamodb_table.violations_log.arn
      }
    ]
  })
}
