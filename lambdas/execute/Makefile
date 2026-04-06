# Makefile for Execute Lambda Container Image

.PHONY: help build push test clean

# Variables
REGION ?= us-east-1
PROJECT_NAME ?= compliance-loop
REPOSITORY_NAME = $(PROJECT_NAME)-execute-lambda
IMAGE_TAG ?= latest

help: ## Show this help message
	@echo "Execute Lambda Container Build Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Environment Variables:"
	@echo "  REGION         AWS region (default: us-east-1)"
	@echo "  PROJECT_NAME   Project name (default: compliance-loop)"
	@echo "  IMAGE_TAG      Docker image tag (default: latest)"
	@echo ""
	@echo "Examples:"
	@echo "  make build                    # Build image locally"
	@echo "  make push                     # Build and push to ECR"
	@echo "  IMAGE_TAG=v1.0.0 make push    # Build and push with custom tag"

build: ## Build Docker image locally (no ECR push)
	@echo "Building Docker image: $(REPOSITORY_NAME):$(IMAGE_TAG)"
	./build.sh --build-only

push: ## Build and push Docker image to ECR
	@echo "Building and pushing to ECR: $(REPOSITORY_NAME):$(IMAGE_TAG)"
	./build.sh

test: ## Test the Docker image locally
	@echo "Testing Docker image..."
	@docker run --rm \
		-e AWS_REGION=$(REGION) \
		$(REPOSITORY_NAME):$(IMAGE_TAG) \
		python -c "import handler; print('Handler imported successfully')"
	@echo "✓ Image test passed"

clean: ## Remove local Docker images
	@echo "Removing local images..."
	@docker rmi $(REPOSITORY_NAME):$(IMAGE_TAG) 2>/dev/null || true
	@echo "✓ Cleanup complete"

inspect: ## Inspect the Docker image
	@docker inspect $(REPOSITORY_NAME):$(IMAGE_TAG)

shell: ## Run a shell in the Docker container
	@docker run --rm -it \
		-e AWS_REGION=$(REGION) \
		--entrypoint /bin/bash \
		$(REPOSITORY_NAME):$(IMAGE_TAG)

size: ## Show Docker image size
	@docker images $(REPOSITORY_NAME):$(IMAGE_TAG) --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"
