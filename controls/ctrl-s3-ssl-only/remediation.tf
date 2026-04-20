terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "bucket_name" { type = string }
variable "region" { type = string }

locals {
  ssl_statement = {
    Sid       = "DenyNonSSLRequests"
    Effect    = "Deny"
    Principal = "*"
    Action    = "s3:*"
    Resource  = [
      "arn:aws:s3:::${var.bucket_name}",
      "arn:aws:s3:::${var.bucket_name}/*"
    ]
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }

  existing_statements = try(
    [for s in jsondecode(data.aws_s3_bucket_policy.current.policy).Statement : s if lookup(s, "Sid", "") != "DenyNonSSLRequests"],
    []
  )
}

data "aws_s3_bucket_policy" "current" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_policy" "ssl_only" {
  bucket = var.bucket_name
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = concat(local.existing_statements, [local.ssl_statement])
  })
}
