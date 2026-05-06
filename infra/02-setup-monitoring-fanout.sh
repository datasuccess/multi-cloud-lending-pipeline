#!/usr/bin/env bash
# =============================================================================
# Phase 2 — monitoring for the six fan-out generators.
#
#   - 6 SQS DLQs (one per Lambda).
#   - 6 × 3 = 18 CW alarms: errors (P1), freshness (P1), low-volume (P2).
#   - 6 DLQ-depth alarms (P2).
#   - Dashboard widgets appended to the existing `lending-pipeline` dashboard.
#
# Reuses the SNS topics created by Phase 1 (`02-setup-monitoring.sh`).
# Idempotent — safe to re-run.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/_env.sh"

[[ -f /tmp/lending-phase1-outputs.env ]] || die \
  "Phase 1 outputs missing — run infra/02-setup-monitoring.sh first."
# shellcheck disable=SC1091
source /tmp/lending-phase1-outputs.env

[[ -n "${SNS_P1_ARN:-}" && -n "${SNS_P2_ARN:-}" ]] || die \
  "SNS topics missing in /tmp/lending-phase1-outputs.env."

# ---------------------------------------------------------------------------
# 1. SQS DLQs (one per Lambda).
# ---------------------------------------------------------------------------
log "[1/3] SQS DLQs"
declare -a DLQ_ARNS=()
for i in "${!PHASE2_SOURCES[@]}"; do
  dlq_name="$(phase2_dlq_name "$i")"
  qurl="$(aws sqs create-queue \
    --queue-name "${dlq_name}" \
    --attributes "MessageRetentionPeriod=1209600,VisibilityTimeout=300" \
    --tags "Project=lending,Phase=2,Source=${PHASE2_SOURCES[$i]}" \
    --region "${REGION}" \
    --query 'QueueUrl' --output text)"
  arn="$(aws sqs get-queue-attributes \
    --queue-url "${qurl}" \
    --attribute-names QueueArn \
    --region "${REGION}" \
    --query 'Attributes.QueueArn' --output text)"
  DLQ_ARNS+=("${arn}")
  log "       ${dlq_name} → ${arn}"
done

# ---------------------------------------------------------------------------
# 2. Alarms — errors / freshness / low-volume / dlq-depth per source.
# ---------------------------------------------------------------------------
log "[2/3] CloudWatch alarms (4 × 6 = 24)"
for i in "${!PHASE2_SOURCES[@]}"; do
  source_name="${PHASE2_SOURCES[$i]}"
  service_name="${PHASE2_SERVICES[$i]}"
  lambda_name="$(phase2_lambda_name "$i")"
  min_rows="${PHASE2_PROD_MIN_ROWS[$i]}"

  # Errors — built-in AWS/Lambda Errors metric.
  aws cloudwatch put-metric-alarm \
    --alarm-name "$(phase2_alarm_errors "$i")" \
    --alarm-description "Lambda raised an exception (validation fail ⇒ no _SUCCESS)" \
    --namespace "AWS/Lambda" \
    --metric-name "Errors" \
    --dimensions "Name=FunctionName,Value=${lambda_name}" \
    --statistic Sum --period 300 --evaluation-periods 1 \
    --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "${SNS_P1_ARN}" \
    --ok-actions "${SNS_P1_ARN}" \
    --region "${REGION}"

  # Low-volume — custom EMF metric, prod thresholds (overridden by 06-set-mode-fanout.sh).
  aws cloudwatch put-metric-alarm \
    --alarm-name "$(phase2_alarm_lowvol "$i")" \
    --alarm-description "Daily file wrote < ${min_rows} rows (${source_name})" \
    --namespace "${POWERTOOLS_NAMESPACE}" \
    --metric-name "rows_written" \
    --dimensions \
        "Name=service,Value=${service_name}" \
        "Name=Source,Value=${source_name}" \
    --statistic Maximum --period 86400 --evaluation-periods 1 \
    --threshold "${min_rows}" --comparison-operator LessThanThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "${SNS_P2_ARN}" \
    --ok-actions "${SNS_P2_ARN}" \
    --region "${REGION}"

  # Freshness — heartbeat custom metric, missing=breaching.
  aws cloudwatch put-metric-alarm \
    --alarm-name "$(phase2_alarm_fresh "$i")" \
    --alarm-description "No successful run in 25h (${source_name})" \
    --namespace "${POWERTOOLS_NAMESPACE}" \
    --metric-name "heartbeat" \
    --dimensions \
        "Name=service,Value=${service_name}" \
        "Name=Source,Value=${source_name}" \
    --statistic Sum --period 3600 --evaluation-periods 25 \
    --threshold 1 --comparison-operator LessThanThreshold \
    --treat-missing-data breaching \
    --alarm-actions "${SNS_P1_ARN}" \
    --ok-actions "${SNS_P1_ARN}" \
    --region "${REGION}"

  # DLQ depth — anything in the DLQ is a failed retry chain.
  dlq_name="$(phase2_dlq_name "$i")"
  aws cloudwatch put-metric-alarm \
    --alarm-name "$(phase2_alarm_dlq "$i")" \
    --alarm-description "DLQ has at least 1 message (failed Lambda retries)" \
    --namespace "AWS/SQS" \
    --metric-name "ApproximateNumberOfMessagesVisible" \
    --dimensions "Name=QueueName,Value=${dlq_name}" \
    --statistic Maximum --period 300 --evaluation-periods 1 \
    --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "${SNS_P2_ARN}" \
    --ok-actions "${SNS_P2_ARN}" \
    --region "${REGION}"

  log "       ${source_name}: errors / low-volume / freshness / dlq-depth"
done

# ---------------------------------------------------------------------------
# 3. Dashboard — append per-source widgets to existing `lending-pipeline`.
#
# Layout: 2 widgets per source, stacked vertically. Phase 1 occupies y=0..11.
# Phase 2 starts at y=12 and grows down. Each source gets a 12-wide rows
# widget on the left and a 12-wide invocations/errors widget on the right.
# ---------------------------------------------------------------------------
log "[3/3] Dashboard widgets"
TMP_DASH="$(mktemp)"
TMP_BODY="$(mktemp)"
aws cloudwatch get-dashboard \
  --dashboard-name "${DASHBOARD_NAME}" \
  --region "${REGION}" \
  --query 'DashboardBody' --output text > "${TMP_BODY}" || \
  echo '{"widgets":[]}' > "${TMP_BODY}"

# Join the parallel arrays into space-separated strings the python sub-process
# can split on. Cleaner than trying to expand bash arrays inside a heredoc.
P2_SOURCES_STR="${PHASE2_SOURCES[*]}"
P2_SERVICES_STR="${PHASE2_SERVICES[*]}"
P2_SHORTS_STR="${PHASE2_SHORT_NAMES[*]}"

python3 - "${TMP_BODY}" "${TMP_DASH}" \
  "${P2_SOURCES_STR}" "${P2_SERVICES_STR}" "${P2_SHORTS_STR}" \
  "${POWERTOOLS_NAMESPACE}" "${REGION}" <<'PY'
import json, sys

body_path, dash_path, sources_str, services_str, shorts_str, namespace, region = sys.argv[1:8]

with open(body_path) as fh:
    dash = json.load(fh)
existing = dash.get("widgets", [])
# Strip any prior Phase 2 widgets so re-runs don't accumulate.
existing = [
    w for w in existing
    if not w.get("properties", {}).get("title", "").endswith(" · phase2")
]

sources = sources_str.split()
services = services_str.split()
lambdas = [f"lending-{s}-generator" for s in shorts_str.split()]

y = 12
for source, service, fn in zip(sources, services, lambdas):
    existing.append({
        "type": "metric", "x": 0, "y": y, "width": 12, "height": 6,
        "properties": {
            "metrics": [
                [namespace, "rows_written", "service", service, "Source", source, {"stat": "Maximum"}]
            ],
            "view": "timeSeries", "stacked": False,
            "region": region,
            "title": f"{source} · rows per run · phase2",
            "period": 86400,
        },
    })
    existing.append({
        "type": "metric", "x": 12, "y": y, "width": 12, "height": 6,
        "properties": {
            "metrics": [
                ["AWS/Lambda", "Invocations", "FunctionName", fn, {"stat": "Sum"}],
                [".", "Errors", ".", ".", {"stat": "Sum"}],
                [".", "Throttles", ".", ".", {"stat": "Sum"}],
            ],
            "view": "timeSeries", "stacked": False,
            "region": region,
            "title": f"{source} · invocations / errors · phase2",
            "period": 300,
        },
    })
    y += 6

with open(dash_path, "w") as fh:
    json.dump({"widgets": existing}, fh)
PY

aws cloudwatch put-dashboard \
  --dashboard-name "${DASHBOARD_NAME}" \
  --dashboard-body "file://${TMP_DASH}" \
  --region "${REGION}" >/dev/null
rm -f "${TMP_DASH}" "${TMP_BODY}"
log "       dashboard updated with 12 fan-out widgets"

# ---------------------------------------------------------------------------
# Outputs — persist DLQ ARNs for the deploy script.
# ---------------------------------------------------------------------------
{
  echo "# Phase 2 fan-out DLQ ARNs (parallel to PHASE2_SOURCES)"
  echo "PHASE2_DLQ_ARNS=(${DLQ_ARNS[*]@Q})"
} >> /tmp/lending-phase1-outputs.env

log "Done."
