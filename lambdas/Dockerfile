# Stage 1: Get OpenTofu binary from official minimal image
# Using OpenTofu's official Docker image as recommended in:
# https://opentofu.org/docs/intro/install/docker/
FROM --platform=linux/arm64 ghcr.io/opentofu/opentofu:minimal AS tofu

# Stage 2: Build Lambda image
FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.13

# Copy OpenTofu binary from minimal image
COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu

# Copy HCL configuration
COPY remediation.tf /opt/remediation/
WORKDIR /opt/remediation

# Pre-cache providers and initialize
RUN tofu providers mirror /opt/providers
RUN tofu init -plugin-dir=/opt/providers

# Copy Lambda handler
COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
