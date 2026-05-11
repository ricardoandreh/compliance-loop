terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" { region = var.region }

variable "bucket_name" { type = string }
variable "region" { type = string }

resource "aws_ebs_encryption_by_default" "remediation" {
  enabled = true
}
