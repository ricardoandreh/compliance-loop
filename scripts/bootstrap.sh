#!/usr/bin/env bash
# Prepare a fresh AWS account for `tofu apply`.
#
# Everything the apply needs but does not create:
#   1. the artifact bucket (state, controls/*/policy.rego and remediation.tf,
#      provider cache): private, versioned, encrypted;
#   2. infra/backend.<env>.hcl and infra/<env>.tfvars, both git-ignored, one
#      pair per environment, so a second copy never overwrites the first;
#   3. the two Lambda layers (OPA and OpenTofu), git-ignored build artifacts;
#   4. the Execute's provider cache, published to the bucket.
#
# Idempotent: re-running keeps an existing bucket and existing tfvars.
#
# A second copy in the same account must go to another region: the CloudTrail
# trail and the EventBridge rule are regional, and two loops in one region would
# both react to every bucket, racing each other. REPRODUCING.md, "Segunda cópia".
#
# Usage: bash scripts/bootstrap.sh [--region us-east-1] [--project compliance-loop]
#                                  [--env dev] [--bucket NAME] [--skip-layers] [--skip-cache]
# --skip-layers keeps lambdas/*/{opa,tofu}_layer.zip already built (same versions).
# Needs: aws cli (authenticated), docker, curl, unzip, zip, file, python >= 3.14.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT_NAME:-compliance-loop}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
BUCKET="${ARTIFACT_BUCKET:-}"
SKIP_CACHE=0
SKIP_LAYERS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --env) ENVIRONMENT="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --skip-cache) SKIP_CACHE=1; shift ;;
    --skip-layers) SKIP_LAYERS=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

ACCOUNT="$(aws sts get-caller-identity --query Account --output text --region "$REGION")"
BUCKET="${BUCKET:-${PROJECT}-artifacts-${ACCOUNT}}"
echo "account ${ACCOUNT}, region ${REGION}, bucket ${BUCKET}"

# 1. Artifact bucket
if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
  echo "bucket exists"
else
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  fi
  echo "bucket created"
fi
aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket "$BUCKET" --region "$REGION" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" --region "$REGION" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# 2. Backend and variables
cat > "$ROOT/infra/backend.${ENVIRONMENT}.hcl" <<EOF
bucket = "${BUCKET}"
key    = "${PROJECT}/${ENVIRONMENT}/terraform.tfstate"
region = "${REGION}"
EOF
echo "wrote infra/backend.${ENVIRONMENT}.hcl"
if [ -f "$ROOT/infra/${ENVIRONMENT}.tfvars" ]; then
  echo "infra/${ENVIRONMENT}.tfvars exists; left as is"
else
  cat > "$ROOT/infra/${ENVIRONMENT}.tfvars" <<EOF
region          = "${REGION}"
environment     = "${ENVIRONMENT}"
project_name    = "${PROJECT}"
artifact_bucket = "${BUCKET}"
EOF
  echo "wrote infra/${ENVIRONMENT}.tfvars"
fi

# 3. Layers
if [ "$SKIP_LAYERS" = 1 ] && [ -f "$ROOT/lambdas/analyze/opa_layer.zip" ] && [ -f "$ROOT/lambdas/execute/tofu_layer.zip" ]; then
  echo "layers kept (--skip-layers)"
else
  bash "$ROOT/scripts/build_analyze.sh"
  bash "$ROOT/scripts/build_execute.sh"
fi

# 4. Provider cache
if [ "$SKIP_CACHE" = 1 ]; then
  echo "provider cache skipped (--skip-cache)"
else
  ARTIFACT_BUCKET="$BUCKET" bash "$ROOT/scripts/build_provider_cache.sh"
fi

cat <<EOF

Next (or: make init apply controls ENV=${ENVIRONMENT}):
  tofu -chdir=infra init -reconfigure -backend-config=backend.${ENVIRONMENT}.hcl
  tofu -chdir=infra apply -var-file=${ENVIRONMENT}.tfvars
  AWS_REGION=${REGION} ENVIRONMENT=${ENVIRONMENT} ./scripts/deploy_control.sh --all
EOF
if [ "$BUCKET" != "${PROJECT}-artifacts-${ACCOUNT}" ]; then
  echo "  export ARTIFACT_BUCKET=${BUCKET}   # the scripts derive the default name; this one is custom"
fi
