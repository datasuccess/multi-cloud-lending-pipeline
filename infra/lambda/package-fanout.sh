#!/usr/bin/env bash
# =============================================================================
# Package one zip per Phase 2 generator. Each zip contains lambdas/shared/ +
# the source's own generator package — siblings are stripped so cross-source
# imports can't accidentally land in production.
#
# Output: build/lending-<short>-generator.zip for each PHASE2_SOURCES entry.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/../_env.sh"

REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
mkdir -p "${REPO_ROOT}/build"

for i in "${!PHASE2_SOURCES[@]}"; do
  source_name="${PHASE2_SOURCES[$i]}"
  package="${PHASE2_PACKAGES[$i]}"
  short="${PHASE2_SHORT_NAMES[$i]}"
  fn_name="lending-${short}-generator"
  work="${REPO_ROOT}/build/fn-${short}"
  zip_path="${REPO_ROOT}/build/${fn_name}.zip"

  log "Packaging ${source_name} → ${zip_path}"
  rm -rf "${work}" "${zip_path}"
  mkdir -p "${work}/lambdas"

  rsync -a \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.gitkeep' \
    "${REPO_ROOT}/lambdas/shared/" "${work}/lambdas/shared/"

  rsync -a \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.gitkeep' \
    "${REPO_ROOT}/lambdas/${package}/" "${work}/lambdas/${package}/"

  # Empty top-level package marker so `import lambdas.shared` works.
  : > "${work}/lambdas/__init__.py"

  ( cd "${work}" && zip -r9 -q "${zip_path}" lambdas )
  size_kb="$(du -k "${zip_path}" | cut -f1)"
  log "       ${zip_path} (${size_kb} KB)"
done

log "Done. ${#PHASE2_SOURCES[@]} zips in ${REPO_ROOT}/build/."
