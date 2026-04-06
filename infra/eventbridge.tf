# EventBridge Rule for S3 PutBucketPublicAccessBlock events
resource "aws_cloudwatch_event_rule" "s3_public_access_block" {
  name        = "${var.project_name}-s3-public-access-block-${var.environment}"
  description = "Capture S3 PutBucketPublicAccessBlock events for compliance monitoring"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {
      eventName = ["PutBucketPublicAccessBlock"]
    }
  })

  tags = {
    Name        = "${var.project_name}-s3-public-access-block"
    Environment = var.environment
    Purpose     = "Trigger compliance loop for S3 public access changes"
  }
}

# EventBridge target with input transformer
resource "aws_cloudwatch_event_target" "step_functions" {
  rule      = aws_cloudwatch_event_rule.s3_public_access_block.name
  target_id = "ComplianceLoopStateMachine"
  arn       = aws_sfn_state_machine.compliance_loop.arn
  role_arn  = aws_iam_role.eventbridge_sfn.arn

  input_transformer {
    input_paths = {
      bucket_name = "$.detail.requestParameters.bucketName"
      account_id  = "$.detail.userIdentity.accountId"
      region      = "$.detail.awsRegion"
      event_time  = "$.detail.eventTime"
    }

    input_template = <<-EOT
    {
      "resource_id": <bucket_name>,
      "resource_arn": "arn:aws:s3:::<bucket_name>",
      "control_id": "ctrl-s3-public-block",
      "account_id": <account_id>,
      "region": <region>,
      "event_time": <event_time>
    }
    EOT
  }
}

# IAM role for EventBridge to invoke Step Functions
resource "aws_iam_role" "eventbridge_sfn" {
  name = "${var.project_name}-eventbridge-sfn-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-eventbridge-sfn"
    Environment = var.environment
  }
}

# IAM policy for EventBridge to start Step Functions execution
resource "aws_iam_role_policy" "eventbridge_sfn" {
  name = "start-step-functions"
  role = aws_iam_role.eventbridge_sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "states:StartExecution"
        Resource = aws_sfn_state_machine.compliance_loop.arn
      }
    ]
  })
}

# Placeholder for Step Functions state machine (to be implemented in task 7)
resource "aws_sfn_state_machine" "compliance_loop" {
  name     = "${var.project_name}-compliance-loop-${var.environment}"
  role_arn = aws_iam_role.step_functions.arn

  definition = jsonencode({
    Comment = "Compliance Loop MAPE-K State Machine - Placeholder"
    StartAt = "Placeholder"
    States = {
      Placeholder = {
        Type = "Pass"
        End  = true
      }
    }
  })

  tags = {
    Name        = "${var.project_name}-compliance-loop"
    Environment = var.environment
    Purpose     = "MAPE-K compliance automation orchestrator"
  }
}

# IAM role for Step Functions (placeholder, will be expanded in task 10)
resource "aws_iam_role" "step_functions" {
  name = "${var.project_name}-step-functions-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "states.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name        = "${var.project_name}-step-functions"
    Environment = var.environment
  }
}

# Basic logging policy for Step Functions
resource "aws_iam_role_policy" "step_functions_logging" {
  name = "cloudwatch-logging"
  role = aws_iam_role.step_functions.id

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
