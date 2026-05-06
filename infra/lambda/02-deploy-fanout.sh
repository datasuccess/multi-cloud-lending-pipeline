#!/usr/bin/env bash
# =============================================================================
# Phase 2 — create-or-update the six fan-out Lambda functions.
#
# Each function:
#   - python3.11, arm64, 512 MB, 60s timeout (matches Phase 1 envelope).
#   - Shares the pyarrow layer published in Phase 1.
#   - Shares the `lending-fanout-generator-role`.
#   - Wired to its own DLQ via DeadLetterConfig (Phase 2 §6).
#   - Triggered by its own EventBridge rule on the prod schedule (§5).
#     Switch to test schedules with `infra/06-set-mode-fanout.sh test`.
#
# Prereqs:
#   - infra/lambda/build-layer.sh    (Phase 1 layer)
#   - infra/01-setup-iam-fanout.sh   (shared role)
#   - infra/02-setup-monitoring-fanout.sh  (DLQs)
#   - infra/lambda/package-fanout.sh (six zips in build/)
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/../_env.sh"

[[ -f /tmp/lending-phase1-outputs.env ]] || die \
  "Run prior infra scripts first — outputs file missing."
# shellcheck disable=SC1091
source /tmp/lending-phase1-outputs.env

[[ -n "${LAYER_VERSION_ARN:-}" ]] || die "LAYER_VERSION_ARN missing — run build-layer.sh first."
[[ -n "${PHASE2_ROLE_ARN:-}" ]]   || die "PHASE2_ROLE_ARN missing — run 01-setup-iam-fanout.sh first."

REPO_ROOT="$(cd "${HERE}/../.." && pwd)"

declare -a LAMBDA_ARNS=()

for i in "${!PHASE2_SOURCES[@]}"; do
  source_name="${PHASE2_SOURCES[$i]}"
  service_name="${PHASE2_SERVICES[$i]}"
  short="${PHASE2_SHORT_NAMES[$i]}"
  fn_name="$(phase2_lambda_name "$i")"
  rule_name="$(phase2_rule_name "$i")"
  dlq_arn="${PHASE2_DLQ_ARNS[$i]}"
  prod_cron="${PHASE2_PROD_CRONS[$i]}"
  prod_min_rows="${PHASE2_PROD_MIN_ROWS[$i]}"
  prod_rows_per_run="${PHASE2_PROD_ROWS_PER_RUN[$i]}"
  handler_path="$(phase2_handler_path "$i")"
  zip_path="${REPO_ROOT}/build/${fn_name}.zip"

  [[ -f "${zip_path}" ]] || die "Function zip missing: ${zip_path} — run package-fanout.sh first."

  log "[${i}/${#PHASE2_SOURCES[@]}] ${fn_name}  (source=${source_name})"

  ENV_VARS="Variables={RAW_BUCKET=${RAW_BUCKET},RAW_BUCKET_URI=${RAW_BUCKET_URI},LOG_LEVEL=INFO,POWERTOOLS_SERVICE_NAME=${service_name},POWERTOOLS_METRICS_NAMESPACE=${POWERTOOLS_NAMESPACE},MIN_ROWS=${prod_min_rows},MODE=prod"
  if [[ "${prod_rows_per_run}" != "0" ]]; then
    ENV_VARS="${ENV_VARS},ROWS_PER_RUN=${prod_rows_per_run}"
  fi
  ENV_VARS="${ENV_VARS}}"

  if aws lambda get-function --function-name "${fn_name}" --region "${REGION}" >/dev/null 2>&1; then
    log "       function exists — updating code + config"
    aws lambda update-function-code \
      --function-name "${fn_name}" \
      --zip-file "fileb://${zip_path}" \
      --region "${REGION}" \
      --query 'FunctionArn' --output text >/dev/null
    aws lambda wait function-updated --function-name "${fn_name}" --region "${REGION}"

    aws lambda update-function-configuration \
      --function-name "${fn_name}" \
      --runtime python3.11 \
      --role "${PHASE2_ROLE_ARN}" \
      --handler "${handler_path}" \
      --timeout 60 \
      --memory-size 512 \
      --environment "${ENV_VARS}" \
      --layers "${LAYER_VERSION_ARN}" \
      --dead-letter-config "TargetArn=${dlq_arn}" \
      --region "${REGION}" \
      --query 'FunctionArn' --output text >/dev/null
    aws lambda wait function-updated --function-name "${fn_name}" --region "${REGION}"
  else
    log "       creating"
    aws lambda create-function \
      --function-name "${fn_name}" \
      --runtime python3.11 \
      --role "${PHASE2_ROLE_ARN}" \
      --handler "${handler_path}" \
      --zip-file "fileb://${zip_path}" \
      --timeout 60 \
      --memory-size 512 \
      --architectures arm64 \
      --environment "${ENV_VARS}" \
      --layers "${LAYER_VERSION_ARN}" \
      --dead-letter-config "TargetArn=${dlq_arn}" \
      --tags "Project=lending,Phase=2,Source=${source_name}" \
      --region "${REGION}" \
      --query 'FunctionArn' --output text >/dev/null
    aws lambda wait function-active --function-name "${fn_name}" --region "${REGION}"
  fi

  aws lambda put-function-concurrency \
    --function-name "${fn_name}" \
    --reserved-concurrent-executions 2 \
    --region "${REGION}" >/dev/null 2>&1 \
    || warn "       skipped reserved-concurrency=2 (account quota; non-fatal)"

  aws logs put-retention-policy \
    --log-group-name "/aws/lambda/${fn_name}" \
    --retention-in-days 7 \
    --region "${REGION}" 2>/dev/null || true

  fn_arn="$(aws lambda get-function --function-name "${fn_name}" \
    --region "${REGION}" --query 'Configuration.FunctionArn' --output text)"
  LAMBDA_ARNS+=("${fn_arn}")

  # EventBridge rule + permission + target.
  aws events put-rule \
    --name "${rule_name}" \
    --schedule-expression "${prod_cron}" \
    --state ENABLED \
    --description "Phase 2 ${source_name} schedule (prod)" \
    --region "${REGION}" >/dev/null

  aws lambda add-permission \
    --function-name "${fn_name}" \
    --statement-id "AllowEventBridgeInvoke-${rule_name}" \
    --action "lambda:InvokeFunction" \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${rule_name}" \
    --region "${REGION}" >/dev/null 2>&1 || log "       (permission already granted)"

  aws events put-targets \
    --rule "${rule_name}" \
    --targets "[{\"Id\":\"${short}-target\",\"Arn\":\"${fn_arn}\",\"Input\":\"{\\\"trigger\\\":\\\"eventbridge:${rule_name}\\\"}\"}]" \
    --region "${REGION}" >/dev/null
  log "       rule ${rule_name} (${prod_cron}) → ${fn_name}"
done

{
  echo "PHASE2_LAMBDA_ARNS=(${LAMBDA_ARNS[*]@Q})"
} >> /tmp/lending-phase1-outputs.env

log "Done. ${#PHASE2_SOURCES[@]} fan-out Lambdas deployed."
log "Smoke test (chain order):"
for i in "${!PHASE2_SOURCES[@]}"; do
  log "  aws lambda invoke --function-name $(phase2_lambda_name "$i") --region ${REGION} \\"
  log "    --cli-binary-format raw-in-base64-out --payload '{\"trigger\":\"manual-smoke\"}' /tmp/out-${PHASE2_SHORT_NAMES[$i]}.json"
done
