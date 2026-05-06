#!/usr/bin/env bash
# =============================================================================
# Phase 2 — flip the six fan-out Lambdas between TEST and PROD shape.
#
#   test:
#     - 6-hourly EventBridge schedules with §5 offsets preserved.
#     - anomaly engine ON (3% skip / 10% undershoot / 5% silent / 5% slow).
#     - per-source MIN_ROWS lowered to match the undershoot range.
#     - low-volume thresholds lowered to match.
#
#   prod:
#     - daily schedules per docs/02-fan-out.md §5.
#     - anomaly engine OFF.
#     - per-source MIN_ROWS / low-volume restored to spec values.
#
# Idempotent. Run AFTER infra/lambda/02-deploy-fanout.sh.
#
# Usage:
#   ./infra/06-set-mode-fanout.sh test
#   ./infra/06-set-mode-fanout.sh prod
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/_env.sh"

[[ -f /tmp/lending-phase1-outputs.env ]] || die "Outputs file missing."
# shellcheck disable=SC1091
source /tmp/lending-phase1-outputs.env

[[ -n "${SNS_P1_ARN:-}" && -n "${SNS_P2_ARN:-}" ]] || die "SNS topics missing."

MODE="${1:-}"
case "${MODE}" in
  test)
    LOG_LEVEL="INFO"
    ANOMALY_VARS="MODE=test,ANOMALY_SKIP_PROB=0.03,ANOMALY_UNDERSHOOT_PROB=0.10,ANOMALY_SILENT_FAIL_PROB=0.05,ANOMALY_SLOW_PROB=0.05"
    declare -n CRONS=PHASE2_TEST_CRONS
    declare -n MIN_ROWS_ARR=PHASE2_TEST_MIN_ROWS
    declare -n ROWS_PER_RUN_ARR=PHASE2_TEST_ROWS_PER_RUN
    LV_PERIOD=21600           # 6h, matches the schedule
    FRESHNESS_PERIOD=21600    # 6h
    FRESHNESS_EVAL=2          # 12h missing = breach
    ;;
  prod)
    LOG_LEVEL="INFO"
    ANOMALY_VARS="MODE=prod"
    declare -n CRONS=PHASE2_PROD_CRONS
    declare -n MIN_ROWS_ARR=PHASE2_PROD_MIN_ROWS
    declare -n ROWS_PER_RUN_ARR=PHASE2_PROD_ROWS_PER_RUN
    LV_PERIOD=86400
    FRESHNESS_PERIOD=3600
    FRESHNESS_EVAL=25
    ;;
  *) die "Usage: $0 {test|prod}";;
esac

log "Switching fan-out Lambdas to ${MODE} mode"

for i in "${!PHASE2_SOURCES[@]}"; do
  source_name="${PHASE2_SOURCES[$i]}"
  service_name="${PHASE2_SERVICES[$i]}"
  fn_name="$(phase2_lambda_name "$i")"
  rule_name="$(phase2_rule_name "$i")"
  cron="${CRONS[$i]}"
  min_rows="${MIN_ROWS_ARR[$i]}"
  rows_per_run="${ROWS_PER_RUN_ARR[$i]}"

  log "[${i}] ${source_name}  cron=${cron}  min_rows=${min_rows}"

  ENV_VARS="Variables={RAW_BUCKET=${RAW_BUCKET},RAW_BUCKET_URI=${RAW_BUCKET_URI},LOG_LEVEL=${LOG_LEVEL},POWERTOOLS_SERVICE_NAME=${service_name},POWERTOOLS_METRICS_NAMESPACE=${POWERTOOLS_NAMESPACE},MIN_ROWS=${min_rows},${ANOMALY_VARS}"
  if [[ "${rows_per_run}" != "0" ]]; then
    ENV_VARS="${ENV_VARS},ROWS_PER_RUN=${rows_per_run}"
  fi
  ENV_VARS="${ENV_VARS}}"

  aws lambda update-function-configuration \
    --function-name "${fn_name}" \
    --environment "${ENV_VARS}" \
    --region "${REGION}" \
    --query 'FunctionArn' --output text >/dev/null
  aws lambda wait function-updated \
    --function-name "${fn_name}" --region "${REGION}"

  aws events put-rule \
    --name "${rule_name}" \
    --schedule-expression "${cron}" \
    --state ENABLED \
    --description "Phase 2 ${source_name} schedule (${MODE} mode)" \
    --region "${REGION}" >/dev/null

  # Re-tune low-volume + freshness alarm thresholds for the active mode.
  aws cloudwatch put-metric-alarm \
    --alarm-name "$(phase2_alarm_lowvol "$i")" \
    --alarm-description "rows_written < ${min_rows} (${source_name}, ${MODE})" \
    --namespace "${POWERTOOLS_NAMESPACE}" \
    --metric-name "rows_written" \
    --dimensions \
        "Name=service,Value=${service_name}" \
        "Name=Source,Value=${source_name}" \
    --statistic Maximum --period "${LV_PERIOD}" --evaluation-periods 1 \
    --threshold "${min_rows}" --comparison-operator LessThanThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "${SNS_P2_ARN}" \
    --ok-actions "${SNS_P2_ARN}" \
    --region "${REGION}"

  aws cloudwatch put-metric-alarm \
    --alarm-name "$(phase2_alarm_fresh "$i")" \
    --alarm-description "No successful run in ${FRESHNESS_EVAL}h (${source_name}, ${MODE})" \
    --namespace "${POWERTOOLS_NAMESPACE}" \
    --metric-name "heartbeat" \
    --dimensions \
        "Name=service,Value=${service_name}" \
        "Name=Source,Value=${source_name}" \
    --statistic Sum --period "${FRESHNESS_PERIOD}" \
    --evaluation-periods "${FRESHNESS_EVAL}" \
    --threshold 1 --comparison-operator LessThanThreshold \
    --treat-missing-data breaching \
    --alarm-actions "${SNS_P1_ARN}" \
    --ok-actions "${SNS_P1_ARN}" \
    --region "${REGION}"
done

log "Done. Fan-out is now ${MODE}."
