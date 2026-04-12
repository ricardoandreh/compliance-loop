#!/usr/bin/env bash
# Setup GitHub credentials in SSM Parameter Store for the Escalation Lambda.
# Usage: ./setup_ssm_github.sh <pat> <owner> <repo>
#
# Example:
#   ./setup_ssm_github.sh ghp_xxxx myorg my-compliance-repo

set -euo pipefail

PAT="${1:?Usage: $0 <pat> <owner> <repo>}"
OWNER="${2:?Usage: $0 <pat> <owner> <repo>}"
REPO="${3:?Usage: $0 <pat> <owner> <repo>}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

aws ssm put-parameter --name /compliance/github/pat   --type SecureString --value "$PAT"   --overwrite --region "$REGION"
aws ssm put-parameter --name /compliance/github/owner --type String       --value "$OWNER" --overwrite --region "$REGION"
aws ssm put-parameter --name /compliance/github/repo  --type String       --value "$REPO"  --overwrite --region "$REGION"

echo "SSM parameters created in $REGION"
