terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }

  backend "s3" {
    bucket = "compliance-mvp-terraform-state"
    key    = "compliance-loop/dev/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

# --- Modules ---

module "knowledge" {
  source       = "./modules/knowledge"
  project_name = var.project_name
  environment  = var.environment
}

module "escalation_queue" {
  source       = "./modules/escalation_queue"
  project_name = var.project_name
  environment  = var.environment
}

module "monitoring" {
  source       = "./modules/monitoring"
  project_name = var.project_name
  environment  = var.environment
}

module "opa_layer" {
  source       = "./modules/opa_layer"
  project_name = var.project_name
  environment  = var.environment
}

module "analyze" {
  source                = "./modules/analyze"
  project_name          = var.project_name
  environment           = var.environment
  exceptions_table_name = module.knowledge.exceptions_table_name
  exceptions_table_arn  = module.knowledge.exceptions_table_arn
  policy_bucket         = "compliance-mvp-terraform-state"
  opa_layer_arn         = module.opa_layer.layer_arn
  account_id            = data.aws_caller_identity.current.account_id
}

module "tofu_layer" {
  source       = "./modules/tofu_layer"
  project_name = var.project_name
  environment  = var.environment
}

module "execute" {
  source             = "./modules/execute"
  project_name       = var.project_name
  environment        = var.environment
  tofu_layer_arn     = module.tofu_layer.layer_arn
  remediation_bucket = "compliance-mvp-terraform-state"
  account_id         = data.aws_caller_identity.current.account_id
  protected_bucket_arns = [
    "arn:aws:s3:::${module.monitoring.cloudtrail_bucket_name}",
    "arn:aws:s3:::compliance-mvp-terraform-state",
  ]
}

module "escalation_lambda" {
  source                    = "./modules/escalation_lambda"
  project_name              = var.project_name
  environment               = var.environment
  region                    = var.region
  account_id                = data.aws_caller_identity.current.account_id
  escalation_queue_arn      = module.escalation_queue.queue_arn
  violations_log_table_name = module.knowledge.violations_log_table_name
  violations_log_table_arn  = module.knowledge.violations_log_table_arn
}

module "orchestration" {
  source                    = "./modules/orchestration"
  project_name              = var.project_name
  environment               = var.environment
  analyze_lambda_arn        = module.analyze.function_arn
  execute_lambda_arn        = module.execute.function_arn
  violations_log_table_name = module.knowledge.violations_log_table_name
  violations_log_table_arn  = module.knowledge.violations_log_table_arn
  escalation_queue_arn      = module.escalation_queue.queue_arn
  escalation_queue_url      = module.escalation_queue.queue_url
  controls_table_name       = module.knowledge.controls_table_name
  controls_table_arn        = module.knowledge.controls_table_arn
  remediation_role_arn      = module.execute.remediation_role_arn
  region                    = var.region
  account_id                = data.aws_caller_identity.current.account_id
}

module "exception_scheduler" {
  source                = "./modules/exception_scheduler"
  project_name          = var.project_name
  environment           = var.environment
  region                = var.region
  account_id            = data.aws_caller_identity.current.account_id
  exceptions_table_name = module.knowledge.exceptions_table_name
  exceptions_table_arn  = module.knowledge.exceptions_table_arn
  exceptions_stream_arn = module.knowledge.exceptions_stream_arn
  state_machine_arn     = module.orchestration.state_machine_arn
}

module "controls_sync" {
  source                = "./modules/controls_sync"
  project_name          = var.project_name
  environment           = var.environment
  controls_table_name   = module.knowledge.controls_table_name
  controls_table_arn    = module.knowledge.controls_table_arn
  controls_stream_arn   = module.knowledge.controls_stream_arn
  eventbridge_rule_name = module.orchestration.eventbridge_rule_name
  eventbridge_rule_arn  = module.orchestration.eventbridge_rule_arn
  remediation_role_arn  = module.execute.remediation_role_arn
}

# --- Outputs ---

output "state_machine_arn" { value = module.orchestration.state_machine_arn }
output "state_machine_name" { value = module.orchestration.state_machine_name }
output "controls_table_name" { value = module.knowledge.controls_table_name }
output "exceptions_table_name" { value = module.knowledge.exceptions_table_name }
output "violations_log_table_name" { value = module.knowledge.violations_log_table_name }
output "eventbridge_rule_name" { value = module.orchestration.eventbridge_rule_name }
output "analyze_function_name" { value = module.analyze.function_name }
output "execute_function_name" { value = module.execute.function_name }
