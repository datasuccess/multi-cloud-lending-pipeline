#!/usr/bin/env bash
# =============================================================================
# Build the shared `lending-pyarrow-layer` Lambda layer.
#
# Strategy B: pip install ARM64 manylinux wheels into ./build/python, zip,
# publish. No Docker required — the three deps (pyarrow / faker /
# aws-lambda-powertools) all ship manylinux2014_aarch64 wheels.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/../_env.sh"

REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
WORK="${REPO_ROOT}/build/layer"
ZIP="${REPO_ROOT}/build/${LAMBDA_LAYER_NAME}.zip"

PYARROW_VER="${PYARROW_VER:-16.1.0}"
FAKER_VER="${FAKER_VER:-25.3.0}"
POWERTOOLS_VER="${POWERTOOLS_VER:-2.43.1}"

log "Building layer ${LAMBDA_LAYER_NAME} (py3.11, arm64)"
log "  pyarrow=${PYARROW_VER}"
log "  faker=${FAKER_VER}"
log "  aws-lambda-powertools=${POWERTOOLS_VER}"

rm -rf "${WORK}" "${ZIP}"
mkdir -p "${WORK}/python"

# Use the host pip — but force the ARM64 manylinux wheels that match the
# Lambda runtime. --only-binary=:all: ensures we never compile from sdist.
pip3 install \
  --target "${WORK}/python" \
  --platform manylinux2014_aarch64 \
  --python-version 3.11 \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  --no-cache-dir \
  --quiet \
  "pyarrow==${PYARROW_VER}" \
  "faker==${FAKER_VER}" \
  "aws-lambda-powertools==${POWERTOOLS_VER}"

# Strip dist-info / tests / __pycache__ to fit comfortably under the 250 MB
# unzipped Lambda limit.
find "${WORK}/python" -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \
  -exec rm -rf {} + 2>/dev/null || true
find "${WORK}/python" -name "*.pyc" -delete 2>/dev/null || true

log "Zipping..."
( cd "${WORK}" && zip -r9 -q "${ZIP}" python )

SIZE_MB="$(du -m "${ZIP}" | cut -f1)"
log "Layer zip: ${ZIP} (${SIZE_MB} MB)"

log "Publishing layer version..."
LAYER_VERSION_ARN="$(aws lambda publish-layer-version \
  --layer-name "${LAMBDA_LAYER_NAME}" \
  --description "pyarrow ${PYARROW_VER} + faker ${FAKER_VER} + powertools ${POWERTOOLS_VER}" \
  --license-info "Apache-2.0" \
  --compatible-runtimes python3.11 \
  --compatible-architectures arm64 \
  --zip-file "fileb://${ZIP}" \
  --region "${REGION}" \
  --query 'LayerVersionArn' --output text)"
log "Published ${LAYER_VERSION_ARN}"

cat >> /tmp/lending-phase1-outputs.env <<EOF
LAYER_VERSION_ARN=${LAYER_VERSION_ARN}
EOF
