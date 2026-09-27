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

resource "aws_ebs_encryption_by_default" "remediation" {
  enabled = true
}
