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

output "topic_arn" { value = aws_sns_topic.alerts.arn }
output "stream_dlq_arn" { value = aws_sqs_queue.stream_failures.arn }
output "stream_dlq_name" { value = aws_sqs_queue.stream_failures.name }
