#!/bin/bash
# Build the Analyze Lambda deployment package
# Bundles: handler.py, OPA binary (linux/arm64), Rego policies
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ANALYZE_DIR="$PROJECT_ROOT/lambdas/analyze"
OPA_VERSION="${OPA_VERSION:-v1.4.2}"
OUT="$ANALYZE_DIR/analyze.zip"

cd "$ANALYZE_DIR"

# Download OPA binary if not present
if [ ! -f opa ]; then
  echo "Downloading OPA $OPA_VERSION..."
  curl -sSL -o opa "https://github.com/open-policy-agent/opa/releases/download/${OPA_VERSION}/opa_linux_arm64_static"
  chmod +x opa
fi

# Copy policies from single source of truth
cp "$PROJECT_ROOT/policies/"*.rego .

# Package
rm -f "$OUT"
zip -j "$OUT" handler.py opa *.rego

# Clean copied rego files (source of truth stays in policies/)
rm -f *.rego

echo "Built: $OUT"
