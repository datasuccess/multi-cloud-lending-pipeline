#!/usr/bin/env bash
# =============================================================================
# Phase 1 — invoke + backfill.
#
#   $ ./03-invoke-and-backfill.sh smoke         # one invocation, today
#   $ ./03-invoke-and-backfill.sh backfill 14   # invoke for last N days
#   $ ./03-invoke-and-backfill.sh verify        # ls partitions + read latest manifest
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/_env.sh"
[[ -f /tmp/lending-phase1-outputs.env ]] && source /tmp/lending-phase1-outputs.env

CMD="${1:-smoke}"

invoke_for_date() {
  local d="$1" tag="${2:-manual}"
  local out
  out="$(mktemp)"
  log "  invoking ingest_date=${d} trigger=${tag}"
  aws lambda invoke \
    --function-name "${LAMBDA_NAME}" \
    --region "${REGION}" \
    --cli-binary-format raw-in-base64-out \
    --payload "{\"ingest_date\":\"${d}\",\"trigger\":\"${tag}\"}" \
    "${out}" >/dev/null
  cat "${out}" | python3 -m json.tool 2>/dev/null || cat "${out}"
  echo
  rm -f "${out}"
}

case "${CMD}" in
  smoke)
    log "Smoke invocation"
    TODAY="$(date -u +%F)"
    invoke_for_date "${TODAY}" "manual-smoke"
    ;;

  backfill)
    DAYS="${2:-14}"
    log "Backfill ${DAYS} days"
    for i in $(seq 1 "${DAYS}"); do
      D="$(date -u -v-"${i}"d +%F 2>/dev/null || date -u -d "${i} days ago" +%F)"
      invoke_for_date "${D}" "manual-backfill"
      sleep 1
    done
    ;;

  verify)
    log "Listing partitions"
    aws s3 ls "s3://${RAW_BUCKET}/raw/loan_applications/" --region "${REGION}"
    log "Latest partition contents:"
    LATEST="$(aws s3 ls "s3://${RAW_BUCKET}/raw/loan_applications/" \
      --region "${REGION}" | awk '{print $2}' | sort | tail -1)"
    aws s3 ls "s3://${RAW_BUCKET}/raw/loan_applications/${LATEST}" \
      --region "${REGION}"
    log "Latest manifest:"
    MANIFEST_KEY="$(aws s3api list-objects-v2 \
      --bucket "${RAW_BUCKET}" \
      --prefix "raw/loan_applications/${LATEST}" \
      --query "Contents[?ends_with(Key, '.manifest.json')].Key | [0]" \
      --output text --region "${REGION}")"
    aws s3 cp "s3://${RAW_BUCKET}/${MANIFEST_KEY}" - --region "${REGION}" \
      | python3 -m json.tool
    log "Pipeline runs ledger (last 5 entries):"
    aws s3 ls "s3://${RAW_BUCKET}/_pipeline_runs/source=loan_applications/" \
      --recursive --region "${REGION}" | tail -5
    ;;

  *)
    die "Unknown command: ${CMD}. Use: smoke | backfill <N> | verify"
    ;;
esac
