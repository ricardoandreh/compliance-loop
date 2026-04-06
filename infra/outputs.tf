output "controls_table_name" {
  description = "Name of the controls DynamoDB table"
  value       = aws_dynamodb_table.controls.name
}

output "controls_table_arn" {
  description = "ARN of the controls DynamoDB table"
  value       = aws_dynamodb_table.controls.arn
}

output "exceptions_table_name" {
  description = "Name of the exceptions DynamoDB table"
  value       = aws_dynamodb_table.exceptions.name
}

output "exceptions_table_arn" {
  description = "ARN of the exceptions DynamoDB table"
  value       = aws_dynamodb_table.exceptions.arn
}

output "exceptions_gsi_name" {
  description = "Name of the control_id GSI on exceptions table"
  value       = "control_id-index"
}

output "violations_log_table_name" {
  description = "Name of the violations_log DynamoDB table"
  value       = aws_dynamodb_table.violations_log.name
}

output "violations_log_table_arn" {
  description = "ARN of the violations_log DynamoDB table"
  value       = aws_dynamodb_table.violations_log.arn
}

output "violations_log_gsi_name" {
  description = "Name of the resource_arn-timestamp GSI on violations_log table"
  value       = "resource_arn-timestamp-index"
}

# CloudTrail outputs
output "cloudtrail_name" {
  description = "Name of the CloudTrail trail"
  value       = aws_cloudtrail.s3_events.name
}

output "cloudtrail_arn" {
  description = "ARN of the CloudTrail trail"
  value       = aws_cloudtrail.s3_events.arn
}

output "cloudtrail_bucket_name" {
  description = "Name of the S3 bucket for CloudTrail logs"
  value       = aws_s3_bucket.cloudtrail_logs.id
}

# EventBridge outputs
output "eventbridge_rule_name" {
  description = "Name of the EventBridge rule for S3 public access events"
  value       = aws_cloudwatch_event_rule.s3_public_access_block.name
}

output "eventbridge_rule_arn" {
  description = "ARN of the EventBridge rule"
  value       = aws_cloudwatch_event_rule.s3_public_access_block.arn
}

# Step Functions outputs
output "state_machine_name" {
  description = "Name of the Step Functions state machine"
  value       = aws_sfn_state_machine.compliance_loop.name
}

output "state_machine_arn" {
  description = "ARN of the Step Functions state machine"
  value       = aws_sfn_state_machine.compliance_loop.arn
}
