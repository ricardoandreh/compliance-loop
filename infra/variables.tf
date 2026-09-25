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

variable "alert_email" {
  description = "Optional e-mail to receive the loop's operational alarms (AWS sends a confirmation first). Empty = no subscription."
  type        = string
  default     = ""
}
