# DynamoDB table for compliance controls
# Requirements: 5.1
resource "aws_dynamodb_table" "controls" {
  name         = "${var.project_name}-controls-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "control_id"

  attribute {
    name = "control_id"
    type = "S"
  }

  tags = {
    Name        = "${var.project_name}-controls-${var.environment}"
    Environment = var.environment
    ManagedBy   = "OpenTofu"
  }
}
