#!/usr/bin/env bash
#
# Deploy a compliance control: uploads artefatos ao S3 e insere na tabela controls.
#
# Usage:
#   ./scripts/deploy_control.sh <control_id>
#   ./scripts/deploy_control.sh --all
#
# Requires: aws cli, jq
# The control must have a directory under controls/<control_id>/ with:
#   - policy.rego
#   - remediation.tf
#   - control.json  (metadata for DynamoDB insert)

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
BUCKET="${CONTROLS_BUCKET:-compliance-mvp-terraform-state}"
TABLE="${CONTROLS_TABLE:-compliance-loop-controls-dev}"
CONTROLS_DIR="$(cd "$(dirname "$0")/../controls" && pwd)"

deploy_control() {
  local control_id="$1"
  local dir="${CONTROLS_DIR}/${control_id}"

  if [[ ! -d "$dir" ]]; then
    echo "ERROR: Directory not found: $dir" >&2
    return 1
  fi

  for f in policy.rego control.json; do
    if [[ ! -f "$dir/$f" ]]; then
      echo "ERROR: Missing $dir/$f" >&2
      return 1
    fi
  done
  # A detective control has no remediation.tf: it classifies and escalates,
  # because its fix is disruptive and belongs to a human (control.json carries
  # "remediation": "manual").
  if grep -q '"manual"' "$dir/control.json"; then
    :
  elif ! grep -q 'remediation_actions' "$dir/control.json"; then
    echo "ERROR: $dir/control.json has no remediation_actions; the Execute scopes each" >&2
    echo "       remediation session to them (see design.md, session policy)" >&2
    return 1
  fi
  if ! grep -q '"manual"' "$dir/control.json" && [[ ! -f "$dir/remediation.tf" ]]; then
    echo "ERROR: Missing $dir/remediation.tf (add \"remediation\": {\"S\": \"manual\"} to make it detective)" >&2
    return 1
  fi

  echo "[$control_id] Uploading artefatos to S3..."
  aws s3 cp "$dir/policy.rego" "s3://${BUCKET}/controls/${control_id}/policy.rego" --region "$REGION" --quiet
  [[ -f "$dir/remediation.tf" ]] && aws s3 cp "$dir/remediation.tf" "s3://${BUCKET}/controls/${control_id}/remediation.tf" --region "$REGION" --quiet

  echo "[$control_id] Inserting into DynamoDB..."
  aws dynamodb put-item \
    --table-name "$TABLE" \
    --item "$(cat "$dir/control.json")" \
    --region "$REGION" > /dev/null

  echo "[$control_id] ✅ Deployed"
}

if [[ "${1:-}" == "--all" ]]; then
  for dir in "$CONTROLS_DIR"/*/; do
    control_id="$(basename "$dir")"
    if [[ -f "$dir/control.json" ]]; then
      deploy_control "$control_id"
    else
      echo "[$control_id] SKIP (no control.json)"
    fi
  done
elif [[ -n "${1:-}" ]]; then
  deploy_control "$1"
else
  echo "Usage: $0 <control_id> | --all"
  echo ""
  echo "Available controls:"
  for dir in "$CONTROLS_DIR"/*/; do
    echo "  $(basename "$dir")"
  done
  exit 1
fi
