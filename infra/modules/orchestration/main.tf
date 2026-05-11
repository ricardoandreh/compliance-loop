variable "project_name" { type = string }
variable "environment" { type = string }
variable "analyze_lambda_arn" { type = string }
variable "execute_lambda_arn" { type = string }
variable "violations_log_table_name" { type = string }
variable "violations_log_table_arn" { type = string }
variable "escalation_queue_arn" { type = string }
variable "escalation_queue_url" { type = string }
variable "controls_table_name" { type = string }
variable "controls_table_arn" { type = string }

# --- EventBridge Rule ---
# Event pattern is dynamically managed by the controls-sync Lambda
# via DynamoDB Stream on INSERT/MODIFY in the controls table.
resource "aws_cloudwatch_event_rule" "s3_public_access_block" {
  name        = "${var.project_name}-s3-public-access-block-${var.environment}"
  description = "Capture S3 compliance events - managed dynamically from controls table"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail      = { eventName = ["PutBucketPublicAccessBlock", "DeleteBucketPublicAccessBlock", "CreateBucket", "PutBucketPolicy", "DeleteBucketPolicy"] }
  })

  tags = { Name = "${var.project_name}-s3-public-access-block", Environment = var.environment }

  lifecycle { ignore_changes = [event_pattern] }
}

resource "aws_cloudwatch_event_target" "step_functions" {
  rule      = aws_cloudwatch_event_rule.s3_public_access_block.name
  target_id = "ComplianceLoopStateMachine"
  arn       = aws_sfn_state_machine.compliance_loop.arn
  role_arn  = aws_iam_role.eventbridge_sfn.arn

  input_transformer {
    input_paths = {
      account_id  = "$.detail.userIdentity.accountId"
      region      = "$.detail.awsRegion"
      event_time  = "$.detail.eventTime"
      event_name  = "$.detail.eventName"
      request_params = "$.detail.requestParameters"
    }

    input_template = <<-EOT
    {
      "account_id": <account_id>,
      "region": <region>,
      "event_time": <event_time>,
      "event_name": <event_name>,
      "request_parameters": <request_params>
    }
    EOT
  }
}

resource "aws_iam_role" "eventbridge_sfn" {
  name = "${var.project_name}-eventbridge-sfn-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${var.project_name}-eventbridge-sfn", Environment = var.environment }
}

resource "aws_iam_role_policy" "eventbridge_sfn" {
  name   = "start-step-functions"
  role   = aws_iam_role.eventbridge_sfn.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "states:StartExecution", Resource = aws_sfn_state_machine.compliance_loop.arn }]
  })
}

# --- Step Functions ---
resource "aws_sfn_state_machine" "compliance_loop" {
  name     = "${var.project_name}-compliance-loop-${var.environment}"
  role_arn = aws_iam_role.step_functions.arn

  definition = templatefile("${path.module}/../../state_machine_definition.json", {
    analyze_lambda_arn   = var.analyze_lambda_arn
    execute_lambda_arn   = var.execute_lambda_arn
    violations_log_table = var.violations_log_table_name
    escalation_queue_url = var.escalation_queue_url
    controls_table       = var.controls_table_name
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_functions.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tags = { Name = "${var.project_name}-compliance-loop", Environment = var.environment }
}

resource "aws_cloudwatch_log_group" "step_functions" {
  name              = "/aws/vendedlogs/states/${var.project_name}-compliance-loop-${var.environment}"
  retention_in_days = 30
  tags              = { Name = "${var.project_name}-step-functions-logs", Environment = var.environment }
}

# --- Step Functions IAM ---
resource "aws_iam_role" "step_functions" {
  name = "${var.project_name}-step-functions-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "states.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${var.project_name}-step-functions", Environment = var.environment }
}

resource "aws_iam_role_policy" "sfn_logging" {
  name = "cloudwatch-logging"
  role = aws_iam_role.step_functions.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = [
      "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"
    ], Resource = "*" }]
  })
}

resource "aws_iam_role_policy" "sfn_lambda" {
  name = "lambda-invocation"
  role = aws_iam_role.step_functions.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = [var.analyze_lambda_arn, var.execute_lambda_arn] }]
  })
}

resource "aws_iam_role_policy" "sfn_dynamodb" {
  name = "dynamodb-operations"
  role = aws_iam_role.step_functions.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = [var.violations_log_table_arn] },
                 { Effect = "Allow", Action = ["dynamodb:Scan"], Resource = [var.controls_table_arn] }]
  })
}

resource "aws_iam_role_policy" "sfn_sqs" {
  name = "sqs-sendmessage"
  role = aws_iam_role.step_functions.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["sqs:SendMessage"], Resource = [var.escalation_queue_arn] }]
  })
}

output "state_machine_arn" { value = aws_sfn_state_machine.compliance_loop.arn }
output "state_machine_name" { value = aws_sfn_state_machine.compliance_loop.name }
output "eventbridge_rule_name" { value = aws_cloudwatch_event_rule.s3_public_access_block.name }
output "eventbridge_rule_arn" { value = aws_cloudwatch_event_rule.s3_public_access_block.arn }
