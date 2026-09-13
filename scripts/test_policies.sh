#!/usr/bin/env bash
# Policy tests, run with the same OPA version the Lambda layer ships.
#
#   1. opa test  — each control's policy_test.rego (policy semantics)
#   2. contract  — every remediation.tf produces a state its policy accepts,
#                  the property the remediation loop needs to terminate
#                  (design.md, L11)
#
# OPA runs from the official image instead of the arm64 layer binary: Rego
# semantics depend on the OPA version, not the CPU architecture. The version is
# read from build_analyze.sh so tests and layer cannot drift apart.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(grep -oP 'OPA_VERSION="\$\{OPA_VERSION:-v?\K[0-9.]+' "$ROOT/scripts/build_analyze.sh")"
IMAGE="openpolicyagent/opa:${VERSION}"
PYTHON="${PYTHON:-$ROOT/lambdas/analyze/.venv/bin/python}"

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE"

echo "== opa test ($IMAGE) =="
docker run --rm -v "$ROOT/controls:/controls:ro" "$IMAGE" test /controls -v

echo
echo "== remediation contract =="
"$PYTHON" -m pytest "$ROOT/tests/contract" -v
