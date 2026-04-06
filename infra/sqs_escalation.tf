# SQS FIFO Queue for escalation (task 8.1)
# This resource is referenced by the Step Functions state machine

resource "aws_sqs_queue" "escalation" {
  name                        = "${var.project_name}-escalation-${var.environment}.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  message_retention_seconds   = 1209600  # 14 days
  visibility_timeout_seconds  = 300      # 5 minutes
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  tags = {
    Name        = "${var.project_name}-escalation"
    Environment = var.environment
    Purpose     = "Queue for failed remediation escalations"
  }
}

# Dead Letter Queue for escalation failures
resource "aws_sqs_queue" "escalation_dlq" {
  name                      = "${var.project_name}-escalation-dlq-${var.environment}.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600  # 14 days

  tags = {
    Name        = "${var.project_name}-escalation-dlq"
    Environment = var.environment
    Purpose     = "DLQ for escalation queue failures"
  }
}

# Redrive policy for escalation queue
resource "aws_sqs_queue_redrive_policy" "escalation" {
  queue_url = aws_sqs_queue.escalation.id
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.escalation_dlq.arn
    maxReceiveCount     = 3
  })
}

# CloudWatch alarm for DLQ messages
resource "aws_cloudwatch_metric_alarm" "escalation_dlq" {
  alarm_name          = "${var.project_name}-escalation-dlq-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Average"
  threshold           = 0
  alarm_description   = "Alert when messages appear in escalation DLQ"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.escalation_dlq.name
  }

  tags = {
    Name        = "${var.project_name}-escalation-dlq-alarm"
    Environment = var.environment
  }
}
