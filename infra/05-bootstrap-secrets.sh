#!/usr/bin/env bash
# =============================================================================
# Phase 1 — Secrets Manager bootstrap.
#
# Creates one project-scoped secret used by the Streamlit monitoring app:
#   - `lending/dev/streamlit-config` — bucket + region + alarm names
#
# Phase 4 will add `lending/dev/pii-investigator-creds` etc.; the namespace is
# documented in docs/secrets-management.md and matches the convention used in
# the sibling /practice projects (see practice/INFRASTRUCTURE_NOTES.md §4).
#
# Idempotent: uses create-secret on first run, put-secret-value on re-runs.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/_env.sh"

ENV_NAME="${ENV_NAME:-dev}"
SECRET_NAME="lending/${ENV_NAME}/streamlit-config"

PAYLOAD="$(cat <<EOF
{
  "raw_bucket": "${RAW_BUCKET}",
  "region": "${REGION}",
  "lambda_name": "${LAMBDA_NAME}",
  "alarm_errors": "${ALARM_ERRORS}",
  "alarm_freshness": "${ALARM_FRESHNESS}",
  "alarm_low_volume": "${ALARM_LOW_VOLUME}",
  "powertools_namespace": "${POWERTOOLS_NAMESPACE}",
  "powertools_service": "${POWERTOOLS_SERVICE_NAME}"
}
EOF
)"

log "Upserting secret ${SECRET_NAME}"

if aws secretsmanager describe-secret \
     --secret-id "${SECRET_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value \
    --secret-id "${SECRET_NAME}" \
    --secret-string "${PAYLOAD}" \
    --region "${REGION}" >/dev/null
  log "Updated existing secret"
else
  aws secretsmanager create-secret \
    --name "${SECRET_NAME}" \
    --description "Streamlit monitoring app config (${ENV_NAME})" \
    --secret-string "${PAYLOAD}" \
    --tags '[{"Key":"Project","Value":"lending"},{"Key":"Phase","Value":"1"}]' \
    --region "${REGION}" >/dev/null
  log "Created secret"
fi

log "Secret ARN:"
aws secretsmanager describe-secret \
  --secret-id "${SECRET_NAME}" --region "${REGION}" \
  --query 'ARN' --output text
