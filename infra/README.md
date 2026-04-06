# Compliance Loop Infrastructure

This directory contains OpenTofu/Terraform configuration for the MVP Compliance Automation Loop infrastructure.

## DynamoDB Tables

### 1. Controls Table
- **Purpose**: Store compliance control metadata
- **Partition Key**: `control_id` (String)
- **Attributes**: 
  - `description`: Control description
  - `cloudtrail_events`: List of CloudTrail events to monitor
  - `rego_policy_package`: OPA/Rego policy package name
  - `resource_type`: AWS resource type (e.g., AWS::S3::Bucket)
  - `severity`: Control severity level
  - `created_at`: Creation timestamp
- **Billing Mode**: PAY_PER_REQUEST

### 2. Exceptions Table
- **Purpose**: Store approved compliance exceptions with automatic expiration
- **Partition Key**: `exception_id` (String/UUID)
- **Global Secondary Index**: `control_id-index` (hash key: `control_id`)
- **TTL Attribute**: `valid_until` (Unix timestamp)
- **Attributes**:
  - `resource_arn`: ARN of the resource with exception
  - `control_id`: Associated control ID
  - `justification`: Reason for exception
  - `approved_by`: Approver identity
  - `created_at`: Creation timestamp
  - `approval_ticket`: Reference ticket (e.g., JIRA-12345)
- **Billing Mode**: PAY_PER_REQUEST

### 3. Violations Log Table
- **Purpose**: Permanent audit log of all compliance evaluations
- **Partition Key**: `violation_id` (String/UUID)
- **Global Secondary Index**: `resource_arn-timestamp-index` (hash key: `resource_arn`, range key: `timestamp`)
- **Attributes**:
  - `control_id`: Associated control ID
  - `state`: Classification state (S_nominal, S_exception, S_violation)
  - `policy_version`: Policy version used for evaluation
  - `exception_id`: Reference to exception (if applicable)
  - `remediation_status`: Status of remediation (success/failed)
  - `remediation_error`: Error details if remediation failed
  - `remediation_timestamp`: When remediation was attempted
  - `github_issue_id`: GitHub issue number for escalated violations
  - `account_id`: AWS account ID
  - `region`: AWS region
- **Billing Mode**: PAY_PER_REQUEST

## Usage

### Initialize OpenTofu
```bash
cd infra
tofu init
```

### Plan Infrastructure Changes
```bash
tofu plan
```

### Apply Infrastructure
```bash
tofu apply
```

### Destroy Infrastructure
```bash
tofu destroy
```

### Custom Variables
You can override default variables:
```bash
tofu apply -var="region=us-west-2" -var="environment=prod"
```

Or create a `terraform.tfvars` file:
```hcl
region      = "us-west-2"
environment = "prod"
project_name = "my-compliance-loop"
```

## Requirements
- OpenTofu >= 1.6.0 or Terraform >= 1.6.0
- AWS Provider ~> 5.0
- AWS credentials configured (via environment variables, AWS CLI, or IAM role)

## Outputs
After applying, the following outputs are available:
- `controls_table_name`: Name of the controls table
- `controls_table_arn`: ARN of the controls table
- `exceptions_table_name`: Name of the exceptions table
- `exceptions_table_arn`: ARN of the exceptions table
- `exceptions_gsi_name`: Name of the control_id GSI
- `violations_log_table_name`: Name of the violations_log table
- `violations_log_table_arn`: ARN of the violations_log table
- `violations_log_gsi_name`: Name of the resource_arn-timestamp GSI
