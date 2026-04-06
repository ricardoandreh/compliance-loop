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
