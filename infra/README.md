# Infrastructure - MVP Compliance Automation Loop

This directory contains OpenTofu/Terraform infrastructure as code for the compliance automation system.

## Overview

The infrastructure implements the MAPE-K pattern with the following components:

- **DynamoDB Tables**: controls, exceptions, violations_log
- **CloudTrail**: S3 event monitoring
- **EventBridge**: Event filtering and routing
- **Step Functions**: Workflow orchestration
- **ECR Repository**: Container image storage for Execute Lambda
- **IAM Roles**: Least-privilege permissions for each component

## Prerequisites

- OpenTofu 1.6+ or Terraform 1.6+
- AWS CLI configured with appropriate credentials
- AWS account with permissions to create resources

## Quick Start

### 1. Initialize

```bash
cd infra/
tofu init
```

### 2. Plan

```bash
tofu plan
```

### 3. Apply

```bash
tofu apply
```

## Deployment Order

For incremental deployment, follow this order:

### Phase 1: Storage Layer
```bash
tofu apply -target=aws_dynamodb_table.controls
tofu apply -target=aws_dynamodb_table.exceptions
tofu apply -target=aws_dynamodb_table.violations_log
```

### Phase 2: Monitoring Layer
```bash
tofu apply -target=aws_cloudtrail.s3_events
tofu apply -target=aws_cloudwatch_event_rule.s3_public_access_block
```

### Phase 3: Container Registry
```bash
tofu apply -target=aws_ecr_repository.execute_lambda
```

After this step, build and push the Execute Lambda container image:
```bash
cd ../lambdas/execute/
./build.sh
cd ../../infra/
```

### Phase 4: Compute and Orchestration
```bash
tofu apply  # Deploy remaining resources
```

## Configuration

### Variables

Edit `variables.tf` or create `terraform.tfvars`:

```hcl
region       = "us-east-1"
environment  = "dev"
project_name = "compliance-loop"
```

### Outputs

After deployment, retrieve important values:

```bash
# Get ECR repository URL
tofu output ecr_repository_url

# Get DynamoDB table names
tofu output controls_table_name
tofu output exceptions_table_name
tofu output violations_log_table_name

# Get Step Functions ARN
tofu output state_machine_arn
```

## ECR Repository

The ECR repository stores the Execute Lambda container image.

### Repository Configuration

- **Name**: `compliance-loop-execute-lambda`
- **Image Scanning**: Enabled on push
- **Encryption**: AES256
- **Lifecycle Policy**: Keep last 5 images
- **Multi-stage Build**: Uses OpenTofu official Docker image

### Build and Push Image

The Execute Lambda uses a multi-stage Docker build with OpenTofu's official minimal image:

```dockerfile
FROM ghcr.io/opentofu/opentofu:1.6-minimal AS tofu
FROM public.ecr.aws/lambda/python:3.12
COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu
```

See [lambdas/execute/README.md](../lambdas/execute/README.md) for detailed instructions.

Quick command:
```bash
cd ../lambdas/execute/
./build.sh
```

## Resource Naming

Resources follow the pattern: `{project_name}-{component}-{environment}`

Examples:
- `compliance-loop-execute-lambda` (ECR repository)
- `compliance-loop-controls` (DynamoDB table)
- `compliance-loop-state-machine` (Step Functions)

## State Management

### Local State (Development)

By default, state is stored locally in `terraform.tfstate`.

### Remote State (Production)

For production, configure S3 backend:

```hcl
# backend.tf
terraform {
  backend "s3" {
    bucket         = "my-terraform-state"
    key            = "compliance-loop/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "terraform-locks"
  }
}
```

## Verification

After deployment, verify resources:

```bash
# Check DynamoDB tables
aws dynamodb list-tables --query 'TableNames[?contains(@, `compliance-loop`)]'

# Check ECR repository
aws ecr describe-repositories --repository-names compliance-loop-execute-lambda

# Check CloudTrail
aws cloudtrail describe-trails --query 'trailList[?Name==`compliance-loop-trail`]'

# Check Step Functions
aws stepfunctions list-state-machines --query 'stateMachines[?contains(name, `compliance-loop`)]'
```

## Cost Estimation

Approximate monthly costs (us-east-1, low volume):

- DynamoDB (on-demand): $1-5
- CloudTrail: $2 (first trail free)
- Step Functions: $0.025 per 1000 transitions
- ECR: $0.10 per GB/month
- Lambda: Pay per invocation

Total estimated: $5-10/month for development

## Cleanup

To destroy all resources:

```bash
# Delete ECR images first
aws ecr batch-delete-image \
  --repository-name compliance-loop-execute-lambda \
  --image-ids imageTag=latest

# Destroy infrastructure
tofu destroy
```

## Troubleshooting

### ECR Repository Already Exists

If you see "RepositoryAlreadyExistsException":

```bash
# Import existing repository
tofu import aws_ecr_repository.execute_lambda compliance-loop-execute-lambda
```

### CloudTrail Bucket Access Denied

Ensure the S3 bucket policy allows CloudTrail to write logs. The `cloudtrail.tf` file includes the necessary bucket policy.

### Step Functions Execution Fails

Check IAM role permissions:

```bash
aws iam get-role --role-name compliance-loop-sfn-role
aws iam list-attached-role-policies --role-name compliance-loop-sfn-role
```

## Files

- `main.tf` - Provider configuration
- `variables.tf` - Input variables
- `outputs.tf` - Output values
- `dynamodb_controls.tf` - Controls table
- `dynamodb_exceptions.tf` - Exceptions table with TTL
- `dynamodb_violations_log.tf` - Violations log table
- `cloudtrail.tf` - CloudTrail configuration
- `eventbridge.tf` - EventBridge rule and input transformer
- `ecr.tf` - ECR repository for Execute Lambda
- `VERIFICATION.md` - Manual verification steps

## References

- [OpenTofu Documentation](https://opentofu.org/docs/)
- [AWS Provider Documentation](https://registry.terraform.io/providers/hashicorp/aws/latest/docs)
- [DynamoDB Best Practices](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/best-practices.html)
