# Execute Lambda - Container Image Build and Deployment

This directory contains the Execute Lambda function that remediates S3 bucket public access violations using OpenTofu.

## Overview

The Execute Lambda is packaged as a container image that includes:
- Python 3.12 runtime
- OpenTofu 1.6+ binary
- Pre-initialized Terraform providers
- Remediation HCL configuration

## Prerequisites

- AWS CLI configured with appropriate credentials
- Docker installed and running
- ECR repository created (via `infra/ecr.tf`)
- Permissions to push to ECR

## Build and Deploy Process

### 1. Deploy ECR Repository

First, ensure the ECR repository exists:

```bash
cd infra/
tofu init
tofu apply -target=aws_ecr_repository.execute_lambda
```

This creates the ECR repository: `compliance-loop-execute-lambda`

### 2. Build and Push Container Image

From the `lambdas/execute/` directory:

```bash
./build.sh
```

The build script will:
1. Get your AWS account ID
2. Construct the ECR repository URL
3. Authenticate Docker to ECR
4. Build the Docker image using docker-compose with OpenTofu and dependencies
5. Push the image to ECR

**Note**: The build process uses `docker-compose` from the project root to build the image.

### 3. Environment Variables

You can customize the build with environment variables:

```bash
# Use a different region
AWS_REGION=us-west-2 ./build.sh

# Use a custom image tag
IMAGE_TAG=v1.0.0 ./build.sh

# Use a different project name
PROJECT_NAME=my-project ./build.sh
```

### 4. Deploy Lambda Function

After the image is pushed to ECR, deploy the Lambda function:

```bash
cd infra/
tofu apply
```

Or update an existing Lambda function:

```bash
aws lambda update-function-code \
  --function-name compliance-loop-execute \
  --image-uri <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/compliance-loop-execute-lambda:latest
```

## Docker Compose Integration

The project uses `docker-compose.yml` at the root to manage the container build:

```yaml
services:
  execute-lambda:
    build:
      context: ./lambdas/execute
      dockerfile: Dockerfile
    image: ${ECR_REPOSITORY_URL}:${IMAGE_TAG}
```

This allows for consistent builds across environments and simplifies the build process.

## OpenTofu via Docker

The Execute Lambda uses OpenTofu's official Docker image via multi-stage build:

```dockerfile
# Stage 1: Get OpenTofu binary
FROM ghcr.io/opentofu/opentofu:1.6-minimal AS tofu

# Stage 2: Lambda image
FROM public.ecr.aws/lambda/python:3.12
COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu
```

**Benefits**:
- No manual binary downloads
- Official OpenTofu distribution
- Consistent versions across builds
- Smaller image size (minimal image contains only the binary)

**Available OpenTofu Docker tags**:
- `ghcr.io/opentofu/opentofu:minimal` - Latest version
- `ghcr.io/opentofu/opentofu:1-minimal` - Latest 1.x
- `ghcr.io/opentofu/opentofu:1.6-minimal` - Latest 1.6.x (used in this project)

Reference: [OpenTofu Docker Installation](https://opentofu.org/docs/intro/install/docker/)

## Manual Build Steps

If you prefer to build manually without the script:

```bash
# Get AWS account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1

# Authenticate Docker to ECR
aws ecr get-login-password --region ${REGION} | \
  docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com

# Build image using docker-compose
export ECR_REPOSITORY_URL="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/compliance-loop-execute-lambda"
export IMAGE_TAG="latest"
docker-compose build execute-lambda

# Push to ECR
docker push ${ECR_REPOSITORY_URL}:${IMAGE_TAG}
```

## Image Contents

### Dockerfile Layers

1. **Stage 1 - OpenTofu**: Uses `ghcr.io/opentofu/opentofu:1.6-minimal` to get the tofu binary
2. **Stage 2 - Lambda Base**: `public.ecr.aws/lambda/python:3.12`
3. **OpenTofu Binary**: Copied from Stage 1 to `/usr/local/bin/tofu`
4. **Remediation Config**: Copies `remediation.tf` to `/opt/remediation/`
5. **Provider Caching**: Pre-downloads AWS provider to `/opt/providers`
6. **Initialization**: Runs `tofu init` to prepare the configuration
7. **Handler**: Copies `handler.py` to Lambda task root

**Multi-stage Build Benefits**:
- Uses official OpenTofu Docker image (no manual downloads)
- Smaller final image size
- Consistent OpenTofu version across builds
- Follows OpenTofu best practices

### Runtime Behavior

When the Lambda executes:
1. Copies HCL config and `.terraform/` to `/tmp/remediation/`
2. Writes `terraform.tfvars.json` with bucket name and region
3. Executes `tofu apply` with `-refresh=false` and `-auto-approve`
4. Uses local ephemeral state at `/tmp/remediation.tfstate`
5. Returns success/failure status to Step Functions

## Troubleshooting

### Build Failures

**Docker not running:**
```bash
# Check Docker status
docker info

# Start Docker (Linux)
sudo systemctl start docker

# On macOS/Windows, start Docker Desktop application
```

**Platform mismatch errors:**
```bash
# Always build with linux/amd64 platform for Lambda
docker build --platform linux/amd64 -t compliance-loop-execute-lambda:latest .
```

**ECR authentication fails:**
```bash
# Verify AWS credentials
aws sts get-caller-identity

# Check ECR repository exists
aws ecr describe-repositories --repository-names compliance-loop-execute-lambda

# Re-authenticate
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin $(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com
```

**Build timeout or slow build:**
```bash
# Use BuildKit for faster builds
export DOCKER_BUILDKIT=1
docker build --platform linux/amd64 --progress=plain -t compliance-loop-execute-lambda:latest .

# Check available disk space
docker system df
docker system prune  # Clean up if needed
```

**OpenTofu download fails:**
```bash
# The Dockerfile uses OpenTofu's official Docker image
# If the build fails, check if the image is accessible
docker pull ghcr.io/opentofu/opentofu:1.6-minimal

# Verify OpenTofu version in the built image
docker run --rm compliance-loop-execute-lambda:latest tofu --version
```

### Deployment Issues

**Lambda function not found:**
```bash
# Create Lambda function first via Terraform
cd infra/
tofu apply
```

**Image pull errors:**
```bash
# Verify Lambda execution role has ECR permissions
aws iam get-role-policy --role-name compliance-loop-execute-role --policy-name ecr-access
```

**OpenTofu execution fails:**
```bash
# Check Lambda logs
aws logs tail /aws/lambda/compliance-loop-execute --follow

# Verify Lambda has sufficient memory (1024 MB) and timeout (45s)
aws lambda get-function-configuration --function-name compliance-loop-execute
```

## Image Lifecycle

The ECR repository has a lifecycle policy that:
- Keeps only the last 5 images
- Automatically expires older images
- Reduces storage costs

To keep more images, modify `infra/ecr.tf`:

```hcl
resource "aws_ecr_lifecycle_policy" "execute_lambda" {
  # ... change countNumber to desired value
}
```

## Security Considerations

- Image scanning is enabled on push (vulnerability detection)
- Images are encrypted at rest with AES256
- Use specific image tags (not `latest`) in production
- Regularly rebuild images to include security patches

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Build Execute Lambda

on:
  push:
    branches: [main]
    paths:
      - 'lambdas/execute/**'

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v2
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: us-east-1
      
      - name: Build and push
        working-directory: lambdas/execute
        run: |
          IMAGE_TAG=${{ github.sha }}
          ./build.sh
```

## References

- [AWS Lambda Container Images](https://docs.aws.amazon.com/lambda/latest/dg/images-create.html)
- [OpenTofu Documentation](https://opentofu.org/docs/)
- [ECR User Guide](https://docs.aws.amazon.com/AmazonECR/latest/userguide/)
