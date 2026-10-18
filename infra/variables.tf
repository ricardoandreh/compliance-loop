variable "region" {
  description = "AWS region for resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (e.g., dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Project name for resource naming"
  type        = string
  default     = "compliance-loop"
}

variable "github_token" {
  description = "GitHub personal access token for creating issues"
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_repo" {
  description = "GitHub repository in format 'owner/repo' for escalation issues"
  type        = string
  default     = ""
}

variable "exception_writer_principals" {
  description = "Principals allowed to assume the exception-writer role (the platform's approval channel); empty trusts the account and leaves it to IAM"
  type        = list(string)
  default     = []
}
