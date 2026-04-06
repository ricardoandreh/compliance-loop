FROM public.ecr.aws/lambda/python:3.12

# Install dependencies for downloading and extracting OpenTofu
RUN dnf install -y unzip curl && dnf clean all

# Install OpenTofu 1.6+ binary
RUN curl -Lo /tmp/tofu.zip https://github.com/opentofu/opentofu/releases/download/v1.6.0/tofu_1.6.0_linux_amd64.zip && \
    unzip /tmp/tofu.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/tofu && \
    rm /tmp/tofu.zip

# Create remediation directory
RUN mkdir -p /opt/remediation

# Copy remediation.tf to /opt/remediation/
COPY remediation.tf /opt/remediation/

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
