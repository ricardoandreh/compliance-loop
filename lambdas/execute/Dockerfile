# Stage 1: Get OpenTofu binary from official minimal image
# Using OpenTofu's official Docker image as recommended in:
# https://opentofu.org/docs/intro/install/docker/
# The minimal image contains only the tofu binary (~50MB)
FROM --platform=linux/arm64 ghcr.io/opentofu/opentofu:minimal AS tofu

# Stage 2: Build Lambda image
FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.13

# Copy OpenTofu binary from minimal image
# This approach is preferred over manual downloads because:
# - Uses official, verified binary
# - Smaller final image size
# - No need for curl/unzip dependencies
# - Consistent across all builds
COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu

# Create remediation directory
RUN mkdir -p /opt/remediation

# Copy remediation templates to /opt/remediation/
COPY remediation.tf remediation_ssl.tf /opt/remediation/

# Set working directory for provider initialization
WORKDIR /opt/remediation

# Pre-cache providers to /opt/providers
RUN tofu providers mirror /opt/providers

# Initialize OpenTofu with cached providers
RUN tofu init -plugin-dir=/opt/providers

# Copy handler.py to Lambda task root
COPY handler.py ${LAMBDA_TASK_ROOT}/

# Set the CMD to your handler
CMD ["handler.lambda_handler"]
