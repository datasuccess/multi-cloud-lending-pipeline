#!/usr/bin/env bash
# =============================================================================
# Build the function zip — just our code, no deps (deps live in the layer).
# Output: build/lending-loan-app-generator.zip with `lambdas/` at root.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/../_env.sh"

REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
WORK="${REPO_ROOT}/build/function"
ZIP="${REPO_ROOT}/build/${LAMBDA_NAME}.zip"

log "Packaging function code → ${ZIP}"
rm -rf "${WORK}" "${ZIP}"
mkdir -p "${WORK}"

# Copy lambdas/ tree minus tests / pycache.
rsync -a \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.gitkeep' \
  "${REPO_ROOT}/lambdas/" "${WORK}/lambdas/"

# Drop generators we're NOT shipping in this Lambda — Phase 1 only deploys
# loan_application_generator. Keep them as empty package markers so other
# generators don't accidentally get pulled in.
for d in customer_generator credit_bureau_generator delinquency_generator \
         loan_decision_generator loan_drawdown_generator payment_generator; do
  if [[ -d "${WORK}/lambdas/${d}" ]]; then
    rm -rf "${WORK}/lambdas/${d}"
  fi
done

( cd "${WORK}" && zip -r9 -q "${ZIP}" lambdas )

SIZE_KB="$(du -k "${ZIP}" | cut -f1)"
log "Function zip: ${ZIP} (${SIZE_KB} KB)"
