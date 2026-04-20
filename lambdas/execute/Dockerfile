# Stage 1: Get OpenTofu binary
FROM --platform=linux/arm64 ghcr.io/opentofu/opentofu:minimal AS tofu

# Stage 2: Build Lambda image
FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.13

COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu

# Pre-initialize: cache provider + generate .terraform and lock file
RUN mkdir -p /tmp/init-seed
COPY provider.tf /tmp/init-seed/
RUN cd /tmp/init-seed \
    && tofu providers mirror /opt/providers \
    && tofu init -plugin-dir=/opt/providers \
    && mv .terraform /opt/tf-dot-terraform \
    && mv .terraform.lock.hcl /opt/tf-lock.hcl \
    && rm -rf /tmp/init-seed

COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
