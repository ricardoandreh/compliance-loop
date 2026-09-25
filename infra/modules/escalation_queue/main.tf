variable "project_name" { type = string }
variable "environment" { type = string }
variable "alarm_topic_arn" { type = string }

resource "aws_sqs_queue" "escalation" {
  name                        = "${var.project_name}-escalation-${var.environment}.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  message_retention_seconds   = 1209600
  visibility_timeout_seconds  = 300
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  tags = { Name = "${var.project_name}-escalation", Environment = var.environment }
}

resource "aws_sqs_queue" "escalation_dlq" {
  name                      = "${var.project_name}-escalation-dlq-${var.environment}.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600

  tags = { Name = "${var.project_name}-escalation-dlq", Environment = var.environment }
}

resource "aws_sqs_queue_redrive_policy" "escalation" {
  queue_url = aws_sqs_queue.escalation.id
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.escalation_dlq.arn
    maxReceiveCount     = 3
  })
}

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
  alarm_actions       = [var.alarm_topic_arn]
  ok_actions          = [var.alarm_topic_arn]
  treat_missing_data  = "notBreaching"

  dimensions = { QueueName = aws_sqs_queue.escalation_dlq.name }

  tags = { Name = "${var.project_name}-escalation-dlq-alarm", Environment = var.environment }
}

output "queue_arn" { value = aws_sqs_queue.escalation.arn }
output "queue_url" { value = aws_sqs_queue.escalation.url }
output "dlq_name" { value = aws_sqs_queue.escalation_dlq.name }
