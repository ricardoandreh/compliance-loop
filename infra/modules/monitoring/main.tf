variable "project_name" { type = string }
variable "environment" { type = string }
variable "exceptions_table_arn" { type = string }

resource "aws_s3_bucket" "cloudtrail_logs" {
  bucket = "${var.project_name}-cloudtrail-logs-${var.environment}"
  tags   = { Name = "${var.project_name}-cloudtrail-logs", Environment = var.environment }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail_logs" {
  bucket                  = aws_s3_bucket.cloudtrail_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "cloudtrail_logs" {
  bucket = aws_s3_bucket.cloudtrail_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.cloudtrail_logs.arn
      },
      {
        Sid       = "AWSCloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.cloudtrail_logs.arn}/*"
        Condition = { StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" } }
      }
    ]
  })
}

resource "aws_cloudtrail" "s3_events" {
  name                          = "${var.project_name}-s3-events-${var.environment}"
  s3_bucket_name                = aws_s3_bucket.cloudtrail_logs.id
  include_global_service_events = false
  is_multi_region_trail         = false
  enable_logging                = true
  # The thesis uses this trail as independent confirmation of what the loop did
  # (every remediation write carries the violation_id as its session name).
  # Log file validation makes that evidence tamper-evident: CloudTrail signs an
  # hourly digest, and `aws cloudtrail validate-logs` proves no file was altered
  # or removed.
  enable_log_file_validation = true

  event_selector {
    read_write_type           = "WriteOnly"
    include_management_events = true
  }

  # Every write to the exceptions table, with the session that made it: the
  # evidence of which channel granted an exception, independent of the row's
  # own approved_by.
  event_selector {
    read_write_type           = "WriteOnly"
    include_management_events = false
    data_resource {
      type   = "AWS::DynamoDB::Table"
      values = [var.exceptions_table_arn]
    }
  }

  tags       = { Name = "${var.project_name}-s3-events", Environment = var.environment }
  depends_on = [aws_s3_bucket_policy.cloudtrail_logs]
}

output "cloudtrail_name" { value = aws_cloudtrail.s3_events.name }
output "cloudtrail_arn" { value = aws_cloudtrail.s3_events.arn }
output "cloudtrail_bucket_name" { value = aws_s3_bucket.cloudtrail_logs.id }
