#!/usr/bin/env bash
# =============================================================================
# Phase 1 — teardown. Reverses everything 00-/01-/02-/lambda/ created.
# Order matters: rules → permissions → Lambda → layer versions → alarms →
# dashboard → CloudTrail → SNS → IAM → S3 (empty + delete) → KMS alias →
# (KMS key requires manual schedule deletion). Budget cleared last.
#
# Idempotent: every step swallows "not found" and prints what's gone.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/_env.sh"

read -rp "TEARDOWN ALL Phase 1 resources in account ${ACCOUNT_ID}? [type 'yes']: " ans
[[ "${ans}" == "yes" ]] || die "Aborted."

silent() { "$@" 2>/dev/null || true; }

log "Removing EventBridge rule"
silent aws events remove-targets --rule "${EVENTBRIDGE_RULE}" --ids loan-app-generator --region "${REGION}"
silent aws events delete-rule --name "${EVENTBRIDGE_RULE}" --region "${REGION}"

log "Removing Lambda function + concurrency"
silent aws lambda delete-function --function-name "${LAMBDA_NAME}" --region "${REGION}"

log "Deleting all layer versions of ${LAMBDA_LAYER_NAME}"
for v in $(aws lambda list-layer-versions --layer-name "${LAMBDA_LAYER_NAME}" \
            --region "${REGION}" --query 'LayerVersions[].Version' --output text 2>/dev/null); do
  silent aws lambda delete-layer-version --layer-name "${LAMBDA_LAYER_NAME}" \
    --version-number "${v}" --region "${REGION}"
done

log "Removing CloudWatch alarms"
silent aws cloudwatch delete-alarms \
  --alarm-names "${ALARM_ERRORS}" "${ALARM_FRESHNESS}" "${ALARM_LOW_VOLUME}" \
  --region "${REGION}"

log "Removing dashboard"
silent aws cloudwatch delete-dashboards --dashboard-names "${DASHBOARD_NAME}" --region "${REGION}"

log "Stopping + deleting CloudTrail"
silent aws cloudtrail stop-logging --name "${CLOUDTRAIL_NAME}" --region "${REGION}"
silent aws cloudtrail delete-trail --name "${CLOUDTRAIL_NAME}" --region "${REGION}"

log "Removing SNS topics"
for t in "${SNS_P1_TOPIC}" "${SNS_P2_TOPIC}"; do
  arn="arn:aws:sns:${REGION}:${ACCOUNT_ID}:${t}"
  silent aws sns delete-topic --topic-arn "${arn}" --region "${REGION}"
done

log "Removing IAM roles + inline policies"
for role in "${LAMBDA_ROLE}" "${PII_LOADER_ROLE}" "${PII_INVESTIGATOR_ROLE}"; do
  for p in $(aws iam list-role-policies --role-name "${role}" \
              --query 'PolicyNames[]' --output text 2>/dev/null); do
    silent aws iam delete-role-policy --role-name "${role}" --policy-name "${p}"
  done
  for p in $(aws iam list-attached-role-policies --role-name "${role}" \
              --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
    silent aws iam detach-role-policy --role-name "${role}" --policy-arn "${p}"
  done
  silent aws iam delete-role --role-name "${role}"
done

log "Emptying + deleting raw bucket and trail bucket"
for b in "${RAW_BUCKET}" "${RAW_BUCKET}-cloudtrail"; do
  if aws s3api head-bucket --bucket "${b}" 2>/dev/null; then
    silent aws s3 rm "s3://${b}" --recursive --region "${REGION}"
    silent aws s3api delete-bucket --bucket "${b}" --region "${REGION}"
  fi
done

log "Removing KMS alias (key enters 30-day pending-deletion only on explicit schedule)"
silent aws kms delete-alias --alias-name "${KMS_ALIAS}" --region "${REGION}"
warn "If you also want the KMS key gone, run:"
warn "  aws kms schedule-key-deletion --key-id <KeyId> --pending-window-in-days 7 --region ${REGION}"

log "Removing AWS Budget"
silent aws budgets delete-budget --account-id "${ACCOUNT_ID}" --budget-name "${BUDGET_NAME}"

log "Local artefacts: rm -rf build/ /tmp/lending-phase1-outputs.env"
rm -rf "$(cd "${HERE}/.." && pwd)/build"
rm -f /tmp/lending-phase1-outputs.env

log "Done."
