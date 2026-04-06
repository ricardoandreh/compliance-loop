# Placeholder for Execute Lambda (to be implemented in task 6)
# This resource is referenced by the Step Functions state machine

resource "aws_lambda_function" "execute" {
  function_name = "${var.project_name}-execute-${var.environment}"
  role          = aws_iam_role.execute_lambda.arn
  
  # Placeholder configuration - will be updated when Lambda container is implemented
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.execute_lambda.repository_url}:latest"
  timeout       = 45
  memory_size   = 1024
  
  ephemeral_storage {
    size = 1024
  }

  environment {
    variables = {
      TOFU_PLUGIN_DIR = "/opt/providers"
    }
  }

  tags = {
    Name        = "${var.project_name}-execute"
    Environment = var.environment
    Purpose     = "Execute component of MAPE-K loop"
  }

  lifecycle {
    ignore_changes = [image_uri]
  }
}

# IAM role for Execute Lambda (to be expanded in task 10)
resource "aws_iam_role" "execute_lambda" {
  name = "${var.project_name}-execute-lambda-${var.environment}"

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
    Name        = "${var.project_name}-execute-lambda"
    Environment = var.environment
  }
}

# Basic logging policy for Execute Lambda
resource "aws_iam_role_policy" "execute_lambda_logging" {
  name = "cloudwatch-logging"
  role = aws_iam_role.execute_lambda.id

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
