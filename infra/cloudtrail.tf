# CloudTrail S3 bucket for logs
resource "aws_s3_bucket" "cloudtrail_logs" {
  bucket = "${var.project_name}-cloudtrail-logs-${var.environment}"

  tags = {
    Name        = "${var.project_name}-cloudtrail-logs"
    Environment = var.environment
    Purpose     = "CloudTrail logs storage"
  }
}

# Block public access to CloudTrail logs bucket
resource "aws_s3_bucket_public_access_block" "cloudtrail_logs" {
  bucket = aws_s3_bucket.cloudtrail_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Bucket policy to allow CloudTrail to write logs
resource "aws_s3_bucket_policy" "cloudtrail_logs" {
  bucket = aws_s3_bucket.cloudtrail_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AWSCloudTrailAclCheck"
        Effect = "Allow"
        Principal = {
          Service = "cloudtrail.amazonaws.com"
        }
        Action   = "s3:GetBucketAcl"
        Resource = aws_s3_bucket.cloudtrail_logs.arn
      },
      {
        Sid    = "AWSCloudTrailWrite"
        Effect = "Allow"
        Principal = {
          Service = "cloudtrail.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.cloudtrail_logs.arn}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-acl" = "bucket-owner-full-control"
          }
        }
      }
    ]
  })
}

# CloudTrail trail for S3 management events
resource "aws_cloudtrail" "s3_events" {
  name                          = "${var.project_name}-s3-events-${var.environment}"
  s3_bucket_name                = aws_s3_bucket.cloudtrail_logs.id
  include_global_service_events = false
  is_multi_region_trail         = false
  enable_logging                = true

  event_selector {
    read_write_type           = "WriteOnly"
    include_management_events = true
  }

  tags = {
    Name        = "${var.project_name}-s3-events"
    Environment = var.environment
    Purpose     = "Capture S3 management events for compliance monitoring"
  }

  depends_on = [aws_s3_bucket_policy.cloudtrail_logs]
}
