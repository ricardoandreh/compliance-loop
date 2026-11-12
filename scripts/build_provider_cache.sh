#!/usr/bin/env bash
# Build the Execute's provider cache and publish it to the artifact bucket.
#
# The AWS provider extracted (780 MB on 6.66.0) does not fit a Lambda layer, so
# the Execute downloads an already-initialised .terraform/ from S3 during init
# and symlinks it into each remediation's work dir (no `tofu init` at runtime).
# This builds that archive from the official linux_arm64 package, signature
# checked by `tofu providers mirror`, so no arm64 emulation is needed.
#
# The provider version is whatever lambdas/execute/provider.tf resolves to
# (~> 6.0, pinned by the lock file the script generates); it must satisfy the
# constraint of every controls/*/remediation.tf.
#
# Usage: ARTIFACT_BUCKET=<bucket> bash scripts/build_provider_cache.sh
# Needs: docker, unzip, aws cli, python >= 3.14 (compression.zstd in the stdlib).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXECUTE_DIR="$(cd "$SCRIPT_DIR/../lambdas/execute" && pwd)"
TOFU_VERSION="${TOFU_VERSION:-1.12.6}"
BUCKET="${ARTIFACT_BUCKET:?set ARTIFACT_BUCKET to the artifact bucket (scripts/bootstrap.sh prints it)}"
KEY="${PROVIDER_CACHE_KEY:-provider-cache/tf-dot-terraform.tar.zst}"
PYTHON="${PYTHON:-python3.14}"

# The archive is zstd; either Python 3.14 (compression.zstd in the stdlib) or
# the zstd CLI produces it.
if "$PYTHON" -c "import compression.zstd" 2>/dev/null; then
  PACK=python
elif command -v zstd >/dev/null; then
  PACK=cli
else
  echo "ERROR: need python3.14 (compression.zstd) or the zstd CLI; set PYTHON=... or install zstd" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp "$EXECUTE_DIR/provider.tf" "$WORK/"
cd "$WORK"

TOFU="docker run --rm -u $(id -u):$(id -g) -e HOME=/w -v $WORK:/w -w /w ghcr.io/opentofu/opentofu:${TOFU_VERSION}"
echo "Resolving the provider with OpenTofu ${TOFU_VERSION}..."
$TOFU providers lock -platform=linux_arm64 -platform=linux_amd64 >/dev/null
$TOFU providers mirror -platform=linux_arm64 /w/mirror >/dev/null

ZIP="$(ls mirror/registry.opentofu.org/hashicorp/aws/terraform-provider-aws_*_linux_arm64.zip)"
V="$(basename "$ZIP" | sed -E 's/terraform-provider-aws_(.*)_linux_arm64\.zip/\1/')"
echo "Provider AWS ${V} (linux_arm64)"

# The layout `tofu init` would have produced, plus the plugin_path the handler
# points the symlinked .terraform at.
D="build/tf-dot-terraform/providers/registry.opentofu.org/hashicorp/aws/${V}"
mkdir -p "$D/linux_arm64"
: > "$D/linux_arm64.lock"
unzip -q "$ZIP" -d "$D/linux_arm64"
echo '["/opt/providers"]' > build/tf-dot-terraform/plugin_path
cp .terraform.lock.hcl build/tf-lock.hcl

if [ "$PACK" = python ]; then
  "$PYTHON" - <<'PY'
import tarfile
from compression import zstd
with zstd.open("tf-dot-terraform.tar.zst", "wb", level=3) as z, tarfile.open(fileobj=z, mode="w|") as tar:
    for name in ("tf-dot-terraform", "tf-lock.hcl"):
        tar.add(f"build/{name}", arcname=name)
PY
else
  tar -C build -cf - tf-dot-terraform tf-lock.hcl | zstd -3 -q -o tf-dot-terraform.tar.zst
fi

aws s3 cp tf-dot-terraform.tar.zst "s3://${BUCKET}/${KEY}"
echo "Published: s3://${BUCKET}/${KEY} ($(du -h tf-dot-terraform.tar.zst | cut -f1), provider AWS ${V})"
