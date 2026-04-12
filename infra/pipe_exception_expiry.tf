# EventBridge Pipe: DynamoDB Streams (exceptions TTL expiry) → Step Functions
# When a TTL-expired exception is removed, triggers re-evaluation via the compliance loop.
# Uses OLD_IMAGE to extract resource_arn and control_id from the deleted item.
#
# NOTE: DynamoDB TTL deletion latency is non-deterministic (up to 48h after expiry).
# The Analyze Lambda's FilterExpression (valid_until > now()) ensures correctness
# regardless of physical deletion timing.

# --- IAM Role for EventBridge Pipe ---
resource "aws_iam_role" "pipe_exception_expiry" {
  name = "${var.project_name}-pipe-exception-expiry-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pipes.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name        = "${var.project_name}-pipe-exception-expiry"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy" "pipe_source" {
  name = "dynamodb-stream-source"
  role = aws_iam_role.pipe_exception_expiry.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:DescribeStream",
        "dynamodb:GetRecords",
        "dynamodb:GetShardIterator",
        "dynamodb:ListStreams"
      ]
      Resource = aws_dynamodb_table.exceptions.stream_arn
    }]
  })
}

resource "aws_iam_role_policy" "pipe_target" {
  name = "stepfunctions-target"
  role = aws_iam_role.pipe_exception_expiry.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = aws_sfn_state_machine.compliance_loop.arn
    }]
  })
}

# --- EventBridge Pipe ---
resource "aws_pipes_pipe" "exception_expiry" {
  name     = "${var.project_name}-exception-expiry-${var.environment}"
  role_arn = aws_iam_role.pipe_exception_expiry.arn

  source = aws_dynamodb_table.exceptions.stream_arn

  # Filter: only REMOVE events (TTL deletions)
  source_parameters {
    dynamodb_stream_parameters {
      starting_position = "LATEST"
      batch_size        = 1
    }

    filter_criteria {
      filter {
        pattern = jsonencode({
          eventName = ["REMOVE"]
        })
      }
    }
  }

  target = aws_sfn_state_machine.compliance_loop.arn

  target_parameters {
    step_function_state_machine_parameters {
      invocation_type = "FIRE_AND_FORGET"
    }

    input_template = <<-EOT
    {
      "resource_arn": <$.dynamodb.OldImage.resource_arn.S>,
      "resource_id": <$.dynamodb.OldImage.resource_arn.S>,
      "control_id": <$.dynamodb.OldImage.control_id.S>,
      "account_id": "${data.aws_caller_identity.current.account_id}",
      "region": "${var.region}",
      "event_time": <$.dynamodb.ApproximateCreationDateTime>,
      "trigger": "exception_ttl_expiry"
    }
    EOT
  }

  tags = {
    Name        = "${var.project_name}-exception-expiry"
    Environment = var.environment
    Purpose     = "Re-evaluate compliance after exception TTL expiry"
  }
}
