# Placeholder for Escalation Lambda (to be implemented in task 9)
# This Lambda is triggered by the SQS escalation queue

resource "aws_lambda_function" "escalation" {
  function_name = "${var.project_name}-escalation-${var.environment}"
  role          = aws_iam_role.escalation_lambda.arn
  
  filename         = "${path.module}/../lambdas/escalation/escalation.zip"
  source_code_hash = filebase64sha256("${path.module}/../lambdas/escalation/escalation.zip")
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 256

  environment {
    variables = {
      VIOLATIONS_TABLE = aws_dynamodb_table.violations_log.name
    }
  }

  tags = {
    Name        = "${var.project_name}-escalation"
    Environment = var.environment
    Purpose     = "Escalation component for failed remediations"
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

# SQS trigger for Escalation Lambda
resource "aws_lambda_event_source_mapping" "escalation_sqs" {
  event_source_arn = aws_sqs_queue.escalation.arn
  function_name    = aws_lambda_function.escalation.arn
  batch_size       = 1
  enabled          = true

  depends_on = [
    aws_iam_role_policy.escalation_lambda_sqs
  ]
}

# IAM role for Escalation Lambda
resource "aws_iam_role" "escalation_lambda" {
  name = "${var.project_name}-escalation-lambda-${var.environment}"

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
    Name        = "${var.project_name}-escalation-lambda"
    Environment = var.environment
  }
}

# CloudWatch Logs policy for Escalation Lambda
resource "aws_iam_role_policy" "escalation_lambda_logging" {
  name = "cloudwatch-logging"
  role = aws_iam_role.escalation_lambda.id

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

# SQS permissions for Escalation Lambda
resource "aws_iam_role_policy" "escalation_lambda_sqs" {
  name = "sqs-access"
  role = aws_iam_role.escalation_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.escalation.arn
      }
    ]
  })
}

# DynamoDB permissions for Escalation Lambda
resource "aws_iam_role_policy" "escalation_lambda_dynamodb" {
  name = "dynamodb-access"
  role = aws_iam_role.escalation_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:UpdateItem"
        ]
        Resource = aws_dynamodb_table.violations_log.arn
      }
    ]
  })
}


# SSM Parameter Store + KMS permissions for GitHub credentials
resource "aws_iam_role_policy" "escalation_lambda_ssm" {
  name = "ssm-github-params"
  role = aws_iam_role.escalation_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/compliance/github/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.region}.amazonaws.com"
          }
        }
      }
    ]
  })
}
