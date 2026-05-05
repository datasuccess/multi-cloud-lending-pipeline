#!/usr/bin/env bash
# =============================================================================
# Phase 1 — chaos test.
#
# Forces a validation failure WITHOUT redeploying: invoke with rows=5. The
# handler's MIN_ROWS=10000 makes that fail post-write validation, the run
# raises, no _SUCCESS is written, AWS/Lambda Errors increments → P1 alarm
# fires within ~5 minutes.
#
# Cleans up: deletes the failed-partition artefacts so backfill / production
# don't see a stuck "in progress" partition.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/_env.sh"
[[ -f /tmp/lending-phase1-outputs.env ]] && source /tmp/lending-phase1-outputs.env

CHAOS_DATE="${CHAOS_DATE:-2099-01-01}"
log "Chaos invoke (ingest_date=${CHAOS_DATE}, rows=5 → expect validation failure)"

OUT="$(mktemp)"
set +e
aws lambda invoke \
  --function-name "${LAMBDA_NAME}" \
  --region "${REGION}" \
  --cli-binary-format raw-in-base64-out \
  --payload "{\"rows\":5,\"ingest_date\":\"${CHAOS_DATE}\",\"trigger\":\"chaos-test\"}" \
  "${OUT}" >/dev/null
RC=$?
set -e

log "Lambda response payload:"
cat "${OUT}"; echo
rm -f "${OUT}"

log "Lambda invoke exit code: ${RC} (non-zero ⇒ FunctionError, expected)"

PREFIX="raw/loan_applications/ingest_date=${CHAOS_DATE}/"
log "Checking partition under ${PREFIX}..."
aws s3 ls "s3://${RAW_BUCKET}/${PREFIX}" --region "${REGION}" || true

if aws s3api head-object \
     --bucket "${RAW_BUCKET}" --key "${PREFIX}_SUCCESS" \
     --region "${REGION}" >/dev/null 2>&1; then
  warn "_SUCCESS exists for chaos partition — this is a BUG, validation gate failed"
else
  log "Confirmed: no _SUCCESS marker in chaos partition (correct)"
fi

log "Wait ~5 min, then check the alarm:"
log "  aws cloudwatch describe-alarms --alarm-names ${ALARM_ERRORS} \\"
log "    --region ${REGION} --query 'MetricAlarms[0].StateValue'"

log
warn "After verifying the alarm fired, clean the chaos partition:"
warn "  aws s3 rm s3://${RAW_BUCKET}/${PREFIX} --recursive --region ${REGION}"
warn "  aws s3 rm s3://${RAW_BUCKET}/_pipeline_runs/source=loan_applications/year=2099/ --recursive --region ${REGION}"
