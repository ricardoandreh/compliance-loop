#!/bin/bash
# Build the Execute Lambda package and its OpenTofu layer (arm64).
#
# The version is pinned here, not "latest": the layer is part of the measured
# path, so which OpenTofu remediated a series must be answerable from the repo.
# The download is checked against the release's SHA256SUMS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXECUTE_DIR="$(cd "$SCRIPT_DIR/../lambdas/execute" && pwd)"
TOFU_VERSION="${TOFU_VERSION:-1.12.6}"
BASE="https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}"
ZIP="tofu_${TOFU_VERSION}_linux_arm64.zip"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

echo "Downloading OpenTofu ${TOFU_VERSION} (linux_arm64)..."
if curl -sSLf --retry 5 --retry-all-errors --retry-delay 5 -o "$ZIP" "$BASE/$ZIP" \
   && curl -sSLf --retry 5 --retry-all-errors --retry-delay 5 -o SHA256SUMS "$BASE/tofu_${TOFU_VERSION}_SHA256SUMS"; then
  grep " ${ZIP}\$" SHA256SUMS | sha256sum -c -
  unzip -q "$ZIP" tofu
else
  # GitHub release assets do go down (504s for hours). The official OCI image
  # ships the same binary; the tag pins the version, and the platform the arch.
  echo "GitHub release unavailable; taking the binary from ghcr.io/opentofu/opentofu:${TOFU_VERSION} (linux/arm64)" >&2
  cid="$(docker create --platform linux/arm64 "ghcr.io/opentofu/opentofu:${TOFU_VERSION}")"
  docker cp "$cid:/usr/local/bin/tofu" ./tofu
  docker rm "$cid" >/dev/null
  chmod +x tofu
fi
if ! file tofu | grep -q "ARM aarch64"; then
  echo "ERROR: tofu binary is not arm64" >&2
  exit 1
fi

# Layer structure: bin/tofu -> /opt/bin/tofu in Lambda. Fixed mtimes and -X
# make the zip byte-identical across rebuilds of the same version.
mkdir -p layer/bin
cp tofu layer/bin/tofu
cp tofu "$EXECUTE_DIR/tofu"
touch -t 202001010000 layer/bin layer/bin/tofu
rm -f "$EXECUTE_DIR/tofu_layer.zip"
(cd layer && TZ=UTC zip -X -qr "$EXECUTE_DIR/tofu_layer.zip" bin/)
echo "Built: tofu_layer.zip ($(du -h "$EXECUTE_DIR/tofu_layer.zip" | cut -f1)), OpenTofu ${TOFU_VERSION}"

cd "$EXECUTE_DIR"
rm -f execute.zip
touch -t 202001010000 handler.py
TZ=UTC zip -X -qj execute.zip handler.py
echo "Built: execute.zip ($(du -h execute.zip | cut -f1))"
