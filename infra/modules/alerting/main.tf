# Where the loop reports its own failures. Created before everything else so
# every module can point at it, and kept free of dependencies for the same reason.

variable "project_name" { type = string }
variable "environment" { type = string }
variable "alert_email" {
  description = "Optional e-mail subscribed to the alerts topic. Empty = topic only (subscribe later)."
  type        = string
  default     = ""
}

resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts-${var.environment}"
  tags = { Name = "${var.project_name}-alerts", Environment = var.environment }
}

# AWS asks the address to confirm the subscription by e-mail before anything is
# delivered, so this cannot page anyone who did not opt in.
resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
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

output "topic_arn" { value = aws_sns_topic.alerts.arn }
output "stream_dlq_arn" { value = aws_sqs_queue.stream_failures.arn }
output "stream_dlq_name" { value = aws_sqs_queue.stream_failures.name }
output "intake_dlq_arn" { value = aws_sqs_queue.intake_failures.arn }
output "intake_dlq_url" { value = aws_sqs_queue.intake_failures.url }
output "intake_dlq_name" { value = aws_sqs_queue.intake_failures.name }
