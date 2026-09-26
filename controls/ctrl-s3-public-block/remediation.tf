terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "bucket_name" {
  type        = string
  description = "Name of the S3 bucket to remediate"
}

variable "region" {
  type        = string
  description = "AWS region where the bucket is located"
}

resource "aws_s3_bucket_public_access_block" "remediation" {
  bucket = var.bucket_name

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}
