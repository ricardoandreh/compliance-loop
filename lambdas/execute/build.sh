#!/bin/bash
set -e

# Build and Push Script for Execute Lambda Container Image
# This script builds the Docker image using docker-compose and pushes it to ECR

# Configuration
REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-compliance-loop}"
REPOSITORY_NAME="${PROJECT_NAME}-execute-lambda"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Execute Lambda Container Build Pipeline ===${NC}"
echo ""

# Step 1: Get AWS account ID
echo -e "${YELLOW}[1/5] Getting AWS account ID...${NC}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
if [ -z "$ACCOUNT_ID" ]; then
    echo -e "${RED}Error: Failed to get AWS account ID. Check AWS credentials.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Account ID: ${ACCOUNT_ID}${NC}"
echo ""

# Step 2: Construct ECR repository URL
ECR_REPOSITORY_URL="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPOSITORY_NAME}"
echo -e "${YELLOW}[2/5] ECR Repository URL: ${ECR_REPOSITORY_URL}${NC}"
echo ""

# Step 3: Authenticate Docker to ECR
echo -e "${YELLOW}[3/5] Authenticating Docker to ECR...${NC}"
aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
if [ $? -ne 0 ]; then
    echo -e "${RED}Error: Failed to authenticate Docker to ECR${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker authenticated to ECR${NC}"
echo ""

# Step 4: Build Docker image using docker-compose
echo -e "${YELLOW}[4/5] Building Docker image with docker-compose...${NC}"
export ECR_REPOSITORY_URL="${ECR_REPOSITORY_URL}"
export IMAGE_TAG="${IMAGE_TAG}"

# Navigate to project root (where docker-compose.yml is)
cd "$(dirname "$0")/../.."

docker-compose build execute-lambda
if [ $? -ne 0 ]; then
    echo -e "${RED}Error: Docker build failed${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker image built successfully (arm64)${NC}"
echo ""

# Step 5: Push to ECR
echo -e "${YELLOW}[5/5] Pushing image to ECR...${NC}"
docker push "${ECR_REPOSITORY_URL}:${IMAGE_TAG}"
if [ $? -ne 0 ]; then
    echo -e "${RED}Error: Failed to push image to ECR${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Image pushed to ECR successfully${NC}"
echo ""

# Summary
echo -e "${GREEN}=== Build Complete ===${NC}"
echo -e "Image URI: ${GREEN}${ECR_REPOSITORY_URL}:${IMAGE_TAG}${NC}"
echo ""
echo "Use this URI to deploy the Lambda function:"
echo "  aws lambda update-function-code \\"
echo "    --function-name <function-name> \\"
echo "    --image-uri ${ECR_REPOSITORY_URL}:${IMAGE_TAG}"
echo ""
