#!/usr/bin/env bash
# =============================================================================
# Phase 1 — toggle the pipeline between TEST and PROD shape.
#
# `test` mode:
#   - hourly EventBridge schedule (cron(0 * * * ? *))
#   - anomaly engine on (skip 3% / undershoot 10% / silent_fail 5% / slow 5%)
#   - MIN_ROWS=1 so undershoot writes pass validation and surface as the
#     low-volume alarm rather than the errors alarm
#   - low-volume alarm threshold lowered to 400 (matches the undershoot range)
#
# `prod` mode:
#   - daily 03:00 UTC schedule (cron(0 3 * * ? *))
#   - anomaly engine off (deterministic runs, no chaos)
#   - MIN_ROWS=10000, low-volume threshold=10000
#
# Run AFTER lambda/deploy.sh + 02-setup-monitoring.sh.
# Idempotent — safe to flip back and forth.
#
# Usage:
#   ./infra/06-set-mode.sh test
#   ./infra/06-set-mode.sh prod
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/_env.sh"
[[ -f /tmp/lending-phase1-outputs.env ]] && source /tmp/lending-phase1-outputs.env

MODE="${1:-}"
case "${MODE}" in
  test)
    SCHEDULE="cron(0 * * * ? *)"   # every hour on the hour
    LOG_LEVEL="INFO"
    ROWS_PER_RUN=2000              # smaller so hourly runs stay cheap
    MIN_ROWS_VAL=1
    LOW_VOLUME_THRESHOLD=400
    ANOMALY_VARS="MODE=test,ANOMALY_SKIP_PROB=0.03,ANOMALY_UNDERSHOOT_PROB=0.10,ANOMALY_SILENT_FAIL_PROB=0.05,ANOMALY_SLOW_PROB=0.05"
    ;;
  prod)
    SCHEDULE="cron(0 3 * * ? *)"   # daily 03:00 UTC
    LOG_LEVEL="INFO"
    ROWS_PER_RUN=12000
    MIN_ROWS_VAL=10000
    LOW_VOLUME_THRESHOLD=10000
    ANOMALY_VARS="MODE=prod"
    ;;
  *) die "Usage: $0 {test|prod}";;
esac

log "Switching pipeline to ${MODE} mode"
log "  schedule           = ${SCHEDULE}"
log "  rows_per_run       = ${ROWS_PER_RUN}"
log "  min_rows           = ${MIN_ROWS_VAL}"
log "  low-volume thresh  = ${LOW_VOLUME_THRESHOLD}"
log "  anomaly vars       = ${ANOMALY_VARS}"

# ---------------------------------------------------------------------------
# 1. Lambda env vars.
# ---------------------------------------------------------------------------
ENV_VARS="Variables={RAW_BUCKET=${RAW_BUCKET},RAW_BUCKET_URI=${RAW_BUCKET_URI},LOG_LEVEL=${LOG_LEVEL},POWERTOOLS_SERVICE_NAME=${POWERTOOLS_SERVICE_NAME},POWERTOOLS_METRICS_NAMESPACE=${POWERTOOLS_NAMESPACE},ROWS_PER_RUN=${ROWS_PER_RUN},MIN_ROWS=${MIN_ROWS_VAL},${ANOMALY_VARS}}"

aws lambda update-function-configuration \
  --function-name "${LAMBDA_NAME}" \
  --environment "${ENV_VARS}" \
  --region "${REGION}" \
  --query 'FunctionArn' --output text >/dev/null
aws lambda wait function-updated \
  --function-name "${LAMBDA_NAME}" --region "${REGION}"
log "Lambda env vars updated"

# ---------------------------------------------------------------------------
# 2. EventBridge schedule.
# ---------------------------------------------------------------------------
aws events put-rule \
  --name "${EVENTBRIDGE_RULE}" \
  --schedule-expression "${SCHEDULE}" \
  --state ENABLED \
  --description "Lending generator schedule (${MODE} mode)" \
  --region "${REGION}" >/dev/null
log "EventBridge schedule set to ${SCHEDULE}"

# ---------------------------------------------------------------------------
# 3. Low-volume alarm threshold (and freshness period when hourly).
# ---------------------------------------------------------------------------
# Test mode evaluates over a 1-hour window because we run hourly; prod uses
# the 24-hour window so a single low-row run doesn't page on a slow day.
if [[ "${MODE}" == "test" ]]; then
  LV_PERIOD=3600
  FRESHNESS_PERIOD=3600
  FRESHNESS_EVAL=2          # 2h missing = breach
else
  LV_PERIOD=86400
  FRESHNESS_PERIOD=3600
  FRESHNESS_EVAL=25         # 25h missing = breach
fi

aws cloudwatch put-metric-alarm \
  --alarm-name "${ALARM_LOW_VOLUME}" \
  --alarm-description "rows_written < ${LOW_VOLUME_THRESHOLD} (${MODE} mode)" \
  --namespace "${POWERTOOLS_NAMESPACE}" \
  --metric-name "rows_written" \
  --dimensions \
      "Name=service,Value=${POWERTOOLS_SERVICE_NAME}" \
      "Name=Source,Value=loan_applications" \
  --statistic Maximum --period "${LV_PERIOD}" --evaluation-periods 1 \
  --threshold "${LOW_VOLUME_THRESHOLD}" \
  --comparison-operator LessThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "${SNS_P2_ARN}" \
  --ok-actions "${SNS_P2_ARN}" \
  --region "${REGION}"

aws cloudwatch put-metric-alarm \
  --alarm-name "${ALARM_FRESHNESS}" \
  --alarm-description "No successful run in ${FRESHNESS_EVAL}h (${MODE} mode)" \
  --namespace "${POWERTOOLS_NAMESPACE}" \
  --metric-name "heartbeat" \
  --dimensions \
      "Name=service,Value=${POWERTOOLS_SERVICE_NAME}" \
      "Name=Source,Value=loan_applications" \
  --statistic Sum --period "${FRESHNESS_PERIOD}" \
  --evaluation-periods "${FRESHNESS_EVAL}" \
  --threshold 1 --comparison-operator LessThanThreshold \
  --treat-missing-data breaching \
  --alarm-actions "${SNS_P1_ARN}" \
  --ok-actions "${SNS_P1_ARN}" \
  --region "${REGION}"
log "Alarms re-tuned for ${MODE} mode"

log "Done. Mode is now ${MODE}."
