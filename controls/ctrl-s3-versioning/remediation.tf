terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region = var.region
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}

variable "bucket_name" { type = string }
variable "region" { type = string }

resource "aws_s3_bucket_versioning" "remediation" {
  bucket = var.bucket_name
  versioning_configuration { status = "Enabled" }
}
