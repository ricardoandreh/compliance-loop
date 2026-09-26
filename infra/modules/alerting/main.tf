# Where the loop reports its own failures. Created before everything else so
# every module can point at it, and kept free of dependencies for the same reason.

variable "project_name" { type = string }
variable "environment" { type = string }

resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts-${var.environment}"
  tags = { Name = "${var.project_name}-alerts", Environment = var.environment }
}

# Records a stream consumer gave up on (after its retries) land here instead of
# being discarded. A DynamoDB stream keeps a record for 24 h; without this, an
# exception whose schedule could not be created would expire with no
# re-evaluation and no trace — which is what C3 showed happens when the
# Scheduler rejects the role. Standard queue: Lambda's on-failure destination
# for stream sources does not accept FIFO.
resource "aws_sqs_queue" "stream_failures" {
  name                      = "${var.project_name}-stream-failures-${var.environment}"
  message_retention_seconds = 1209600
  tags                      = { Name = "${var.project_name}-stream-failures", Environment = var.environment }
}

# Detections the intake rule matched but could not turn into an execution.
# EventBridge drops such an event once its retries run out — at once, for a
# permission error — and the violation it carried is never evaluated. Here the
# input the rule built is kept for 14 days, with the refusal in the message
# attributes; scripts/replay_intake.py starts the executions it should have
# started. The queue policy, which names the rule, lives next to the rule in the
# orchestration module.
resource "aws_sqs_queue" "intake_failures" {
  name                      = "${var.project_name}-intake-failures-${var.environment}"
  message_retention_seconds = 1209600
  tags                      = { Name = "${var.project_name}-intake-failures", Environment = var.environment }
}

# Alarm transitions become GitHub issues, opened by the same Lambda that opens
# the escalation issues: one channel for everything a person has to act on.
# The topic delivers to a queue, not to the Lambda, so a GitHub outage delays
# the notifications instead of losing them — a transition the Lambda cannot
# deliver is retried every visibility timeout for as long as the queue keeps
# it. No DLQ: its alarm would notify through this same channel.
resource "aws_sqs_queue" "alarm_notifications" {
  name                      = "${var.project_name}-alarm-notifications-${var.environment}"
  message_retention_seconds = 1209600
  # 6x the escalation Lambda's 30 s timeout, as AWS advises for SQS triggers.
  visibility_timeout_seconds = 180
  tags                       = { Name = "${var.project_name}-alarm-notifications", Environment = var.environment }
}

resource "aws_sqs_queue_policy" "alarm_notifications" {
  queue_url = aws_sqs_queue.alarm_notifications.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.alarm_notifications.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_sns_topic.alerts.arn } }
    }]
  })
}

# Raw delivery: the message body is the alarm's own JSON, not the SNS envelope.
resource "aws_sns_topic_subscription" "alarm_notifications" {
  topic_arn            = aws_sns_topic.alerts.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.alarm_notifications.arn
  raw_message_delivery = true
}

output "topic_arn" { value = aws_sns_topic.alerts.arn }
output "alarm_queue_arn" { value = aws_sqs_queue.alarm_notifications.arn }
output "alarm_queue_name" { value = aws_sqs_queue.alarm_notifications.name }
output "stream_dlq_arn" { value = aws_sqs_queue.stream_failures.arn }
output "stream_dlq_name" { value = aws_sqs_queue.stream_failures.name }
output "intake_dlq_arn" { value = aws_sqs_queue.intake_failures.arn }
output "intake_dlq_url" { value = aws_sqs_queue.intake_failures.url }
output "intake_dlq_name" { value = aws_sqs_queue.intake_failures.name }
