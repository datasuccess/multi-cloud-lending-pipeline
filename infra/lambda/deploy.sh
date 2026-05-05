#!/usr/bin/env bash
# =============================================================================
# Create or update the Lambda function with the latest code zip + layer
# version, then create/update the daily EventBridge rule.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/../_env.sh"

if [[ -f /tmp/lending-phase1-outputs.env ]]; then
  # shellcheck disable=SC1091
  source /tmp/lending-phase1-outputs.env
else
  die "Run prior phase scripts first."
fi

REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
ZIP="${REPO_ROOT}/build/${LAMBDA_NAME}.zip"
[[ -f "${ZIP}" ]] || die "Function zip missing: ${ZIP}. Run package-function.sh first."
[[ -n "${LAYER_VERSION_ARN:-}" ]] || die "LAYER_VERSION_ARN missing. Run build-layer.sh first."

# Initial deploy is prod-shaped: anomaly engine off, daily cron, strict
# 10k row floor. Switch to test mode (hourly + chaos) with `06-set-mode.sh test`.
ENV_VARS="Variables={RAW_BUCKET=${RAW_BUCKET},RAW_BUCKET_URI=${RAW_BUCKET_URI},LOG_LEVEL=INFO,POWERTOOLS_SERVICE_NAME=${POWERTOOLS_SERVICE_NAME},POWERTOOLS_METRICS_NAMESPACE=${POWERTOOLS_NAMESPACE},ROWS_PER_RUN=12000,MIN_ROWS=10000,MODE=prod}"

log "[1/3] Deploying Lambda ${LAMBDA_NAME}"
if aws lambda get-function --function-name "${LAMBDA_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  log "       function exists — updating code + config"

  aws lambda update-function-code \
    --function-name "${LAMBDA_NAME}" \
    --zip-file "fileb://${ZIP}" \
    --region "${REGION}" \
    --query 'FunctionArn' --output text >/dev/null

  # Wait for the code update to settle before pushing config (otherwise the
  # second update gets ResourceConflictException).
  aws lambda wait function-updated \
    --function-name "${LAMBDA_NAME}" --region "${REGION}"

  # `update-function-configuration` doesn't accept --architectures; the value
  # was fixed at create-time and is immutable post hoc.
  aws lambda update-function-configuration \
    --function-name "${LAMBDA_NAME}" \
    --runtime python3.11 \
    --role "${LAMBDA_ROLE_ARN}" \
    --handler "lambdas.loan_application_generator.handler.lambda_handler" \
    --timeout 60 \
    --memory-size 512 \
    --environment "${ENV_VARS}" \
    --layers "${LAYER_VERSION_ARN}" \
    --region "${REGION}" \
    --query 'FunctionArn' --output text >/dev/null

  aws lambda wait function-updated \
    --function-name "${LAMBDA_NAME}" --region "${REGION}"

  # Reserved concurrency is best-effort: a fresh account starts with 10 total
  # unreserved, and the API refuses to drop unreserved below 10. Skip silently.
  aws lambda put-function-concurrency \
    --function-name "${LAMBDA_NAME}" \
    --reserved-concurrent-executions 2 \
    --region "${REGION}" >/dev/null 2>&1 \
    || warn "       skipped reserved-concurrency=2 (account quota too small; non-fatal)"
else
  log "       function does not exist — creating"
  aws lambda create-function \
    --function-name "${LAMBDA_NAME}" \
    --runtime python3.11 \
    --role "${LAMBDA_ROLE_ARN}" \
    --handler "lambdas.loan_application_generator.handler.lambda_handler" \
    --zip-file "fileb://${ZIP}" \
    --timeout 60 \
    --memory-size 512 \
    --architectures arm64 \
    --environment "${ENV_VARS}" \
    --layers "${LAYER_VERSION_ARN}" \
    --tags Project=lending,Phase=1 \
    --region "${REGION}" \
    --query 'FunctionArn' --output text >/dev/null

  aws lambda wait function-active \
    --function-name "${LAMBDA_NAME}" --region "${REGION}"

  # Reserved concurrency is best-effort: a fresh account starts with 10 total
  # unreserved, and the API refuses to drop unreserved below 10. Skip silently.
  aws lambda put-function-concurrency \
    --function-name "${LAMBDA_NAME}" \
    --reserved-concurrent-executions 2 \
    --region "${REGION}" >/dev/null 2>&1 \
    || warn "       skipped reserved-concurrency=2 (account quota too small; non-fatal)"
fi

# Cap log retention at 7 days now that the log group exists.
aws logs put-retention-policy \
  --log-group-name "/aws/lambda/${LAMBDA_NAME}" \
  --retention-in-days 7 \
  --region "${REGION}" 2>/dev/null || true

LAMBDA_ARN="$(aws lambda get-function --function-name "${LAMBDA_NAME}" \
  --region "${REGION}" --query 'Configuration.FunctionArn' --output text)"
log "       Lambda ARN: ${LAMBDA_ARN}"

# ---------------------------------------------------------------------------
# 2. EventBridge rule — daily at 03:00 UTC.
# ---------------------------------------------------------------------------
log "[2/3] EventBridge ${EVENTBRIDGE_RULE} (cron(0 3 * * ? *))"
aws events put-rule \
  --name "${EVENTBRIDGE_RULE}" \
  --schedule-expression "cron(0 3 * * ? *)" \
  --state ENABLED \
  --description "Daily 03:00 UTC trigger for lending-loan-app-generator" \
  --region "${REGION}" >/dev/null

aws lambda add-permission \
  --function-name "${LAMBDA_NAME}" \
  --statement-id "AllowEventBridgeInvoke-${EVENTBRIDGE_RULE}" \
  --action "lambda:InvokeFunction" \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${EVENTBRIDGE_RULE}" \
  --region "${REGION}" >/dev/null 2>&1 || log "       (permission already granted)"

aws events put-targets \
  --rule "${EVENTBRIDGE_RULE}" \
  --targets "[{\"Id\":\"loan-app-generator\",\"Arn\":\"${LAMBDA_ARN}\",\"Input\":\"{\\\"trigger\\\":\\\"eventbridge:${EVENTBRIDGE_RULE}\\\"}\"}]" \
  --region "${REGION}" >/dev/null
log "       rule + target wired"

# ---------------------------------------------------------------------------
# 3. Outputs.
# ---------------------------------------------------------------------------
cat >> /tmp/lending-phase1-outputs.env <<EOF
LAMBDA_ARN=${LAMBDA_ARN}
EOF

log "[3/3] Done."
log "Smoke test:"
log "  aws lambda invoke --function-name ${LAMBDA_NAME} --region ${REGION} \\"
log "    --cli-binary-format raw-in-base64-out --payload '{\"trigger\":\"manual-smoke\"}' /tmp/out.json \\"
log "  && cat /tmp/out.json"
