#!/bin/bash
# Build the Analyze Lambda deployment package and OPA Layer
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ANALYZE_DIR="$PROJECT_ROOT/lambdas/analyze"
OPA_VERSION="${OPA_VERSION:-v1.21.0}"

cd "$ANALYZE_DIR"

# --- OPA Layer ---

if [ ! -f opa ]; then
  echo "Downloading OPA $OPA_VERSION..."
  curl -sSL -o opa "https://github.com/open-policy-agent/opa/releases/download/${OPA_VERSION}/opa_linux_arm64_static"
  chmod +x opa
fi

if ! file opa | grep -q "ARM aarch64"; then
  echo "ERROR: OPA binary is not arm64. Delete ./opa and re-run." >&2
  exit 1
fi

# Layer structure: bin/opa → available at /opt/bin/opa in Lambda
rm -rf /tmp/opa_layer
mkdir -p /tmp/opa_layer/bin
cp opa /tmp/opa_layer/bin/opa
rm -f opa_layer.zip
(cd /tmp/opa_layer && zip -r "$ANALYZE_DIR/opa_layer.zip" bin/)
rm -rf /tmp/opa_layer
echo "Built: opa_layer.zip ($(du -h opa_layer.zip | cut -f1))"

# --- Analyze Lambda (handler only) ---

rm -f analyze.zip
zip -j analyze.zip handler.py
echo "Built: analyze.zip ($(du -h analyze.zip | cut -f1))"
