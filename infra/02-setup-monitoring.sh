#!/usr/bin/env bash
# =============================================================================
# Phase 1 — monitoring:
#   - SNS topics (P1 page, P2 email) + email subscription
#   - 3 CW alarms: errors (P1), freshness (P1), low-volume (P2)
#   - CloudWatch dashboard `lending-pipeline`
#   - CloudTrail trail with KMS+S3 data events
# Idempotent.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/_env.sh"

if [[ -f /tmp/lending-phase1-outputs.env ]]; then
  # shellcheck disable=SC1091
  source /tmp/lending-phase1-outputs.env
else
  die "Run 00-setup-foundations.sh + 01-setup-iam.sh first."
fi

# ---------------------------------------------------------------------------
# 1. SNS topics + email subscriptions.
# ---------------------------------------------------------------------------
ensure_topic() {
  local name="$1"
  local arn
  arn="$(aws sns create-topic --name "${name}" --region "${REGION}" \
    --query 'TopicArn' --output text)"
  aws sns tag-resource --resource-arn "${arn}" --region "${REGION}" \
    --tags Key=Project,Value=lending Key=Phase,Value=1 || true
  echo "${arn}"
}

log "[1/4] SNS topics"
SNS_P1_ARN="$(ensure_topic "${SNS_P1_TOPIC}")"
SNS_P2_ARN="$(ensure_topic "${SNS_P2_TOPIC}")"
log "       P1=${SNS_P1_ARN}"
log "       P2=${SNS_P2_ARN}"

ensure_email_sub() {
  local topic_arn="$1" email="$2"
  local existing
  existing="$(aws sns list-subscriptions-by-topic \
    --topic-arn "${topic_arn}" --region "${REGION}" \
    --query "Subscriptions[?Endpoint=='${email}'].SubscriptionArn | [0]" \
    --output text)"
  if [[ -n "${existing}" && "${existing}" != "None" ]]; then
    log "       ${email} already subscribed to ${topic_arn##*:}"
    return
  fi
  aws sns subscribe --topic-arn "${topic_arn}" \
    --protocol email --notification-endpoint "${email}" \
    --region "${REGION}" >/dev/null
  log "       subscribed ${email} to ${topic_arn##*:} (CONFIRM in inbox)"
}
ensure_email_sub "${SNS_P1_ARN}" "${ALERT_EMAIL}"
ensure_email_sub "${SNS_P2_ARN}" "${ALERT_EMAIL}"

# ---------------------------------------------------------------------------
# 2. CW alarms (errors, freshness, low-volume).
# ---------------------------------------------------------------------------
log "[2/4] CloudWatch alarms"

# Errors: built-in AWS/Lambda Errors metric, sum >=1 over 5 min => P1.
aws cloudwatch put-metric-alarm \
  --alarm-name "${ALARM_ERRORS}" \
  --alarm-description "Lambda raised an exception (e.g. validation failure ⇒ no _SUCCESS)" \
  --namespace "AWS/Lambda" \
  --metric-name "Errors" \
  --dimensions "Name=FunctionName,Value=${LAMBDA_NAME}" \
  --statistic Sum --period 300 --evaluation-periods 1 \
  --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "${SNS_P1_ARN}" \
  --ok-actions "${SNS_P1_ARN}" \
  --region "${REGION}"
log "       ${ALARM_ERRORS} (P1)"

# Low-volume: rows_written < 10000 (custom EMF metric from Powertools).
# Dimensions: service=loan-app-generator, Source=loan_applications.
aws cloudwatch put-metric-alarm \
  --alarm-name "${ALARM_LOW_VOLUME}" \
  --alarm-description "Daily file wrote < 10,000 rows" \
  --namespace "${POWERTOOLS_NAMESPACE}" \
  --metric-name "rows_written" \
  --dimensions \
      "Name=service,Value=${POWERTOOLS_SERVICE_NAME}" \
      "Name=Source,Value=loan_applications" \
  --statistic Maximum --period 86400 --evaluation-periods 1 \
  --threshold 10000 --comparison-operator LessThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "${SNS_P2_ARN}" \
  --ok-actions "${SNS_P2_ARN}" \
  --region "${REGION}"
log "       ${ALARM_LOW_VOLUME} (P2)"

# Freshness: heartbeat custom EMF metric. Treat missing as breaching: if no
# heartbeat in 25 hours => P1. Period 3600s, 25 evaluation periods.
aws cloudwatch put-metric-alarm \
  --alarm-name "${ALARM_FRESHNESS}" \
  --alarm-description "No successful run in the last 25 hours" \
  --namespace "${POWERTOOLS_NAMESPACE}" \
  --metric-name "heartbeat" \
  --dimensions \
      "Name=service,Value=${POWERTOOLS_SERVICE_NAME}" \
      "Name=Source,Value=loan_applications" \
  --statistic Sum --period 3600 --evaluation-periods 25 \
  --threshold 1 --comparison-operator LessThanThreshold \
  --treat-missing-data breaching \
  --alarm-actions "${SNS_P1_ARN}" \
  --ok-actions "${SNS_P1_ARN}" \
  --region "${REGION}"
log "       ${ALARM_FRESHNESS} (P1, missing=breaching)"

# ---------------------------------------------------------------------------
# 3. Dashboard.
# ---------------------------------------------------------------------------
log "[3/4] CloudWatch dashboard ${DASHBOARD_NAME}"
TMP_DASH="$(mktemp)"
cat > "${TMP_DASH}" <<JSON
{
  "widgets": [
    {
      "type": "metric", "x": 0, "y": 0, "width": 12, "height": 6,
      "properties": {
        "metrics": [
          ["${POWERTOOLS_NAMESPACE}", "rows_written", "service", "${POWERTOOLS_SERVICE_NAME}", "Source", "loan_applications", {"stat": "Maximum"}]
        ],
        "view": "timeSeries", "stacked": false,
        "region": "${REGION}", "title": "loan_applications · rows per run", "period": 86400
      }
    },
    {
      "type": "metric", "x": 12, "y": 0, "width": 12, "height": 6,
      "properties": {
        "metrics": [
          ["${POWERTOOLS_NAMESPACE}", "duration_ms", "service", "${POWERTOOLS_SERVICE_NAME}", "Source", "loan_applications", {"stat": "Average"}],
          [".", "bytes_written", ".", ".", ".", ".", {"stat": "Average", "yAxis": "right"}]
        ],
        "view": "timeSeries", "stacked": false,
        "region": "${REGION}", "title": "duration vs bytes", "period": 86400
      }
    },
    {
      "type": "metric", "x": 0, "y": 6, "width": 12, "height": 6,
      "properties": {
        "metrics": [
          ["AWS/Lambda", "Invocations", "FunctionName", "${LAMBDA_NAME}", {"stat": "Sum"}],
          [".", "Errors", ".", ".", {"stat": "Sum"}],
          [".", "Throttles", ".", ".", {"stat": "Sum"}]
        ],
        "view": "timeSeries", "stacked": false,
        "region": "${REGION}", "title": "Lambda invocations / errors / throttles", "period": 300
      }
    },
    {
      "type": "metric", "x": 12, "y": 6, "width": 12, "height": 6,
      "properties": {
        "metrics": [
          ["${POWERTOOLS_NAMESPACE}", "heartbeat", "service", "${POWERTOOLS_SERVICE_NAME}", "Source", "loan_applications", {"stat": "Sum"}]
        ],
        "view": "timeSeries", "stacked": false,
        "region": "${REGION}", "title": "heartbeat (1=ok)", "period": 3600
      }
    }
  ]
}
JSON
aws cloudwatch put-dashboard \
  --dashboard-name "${DASHBOARD_NAME}" \
  --dashboard-body "file://${TMP_DASH}" \
  --region "${REGION}" >/dev/null
rm -f "${TMP_DASH}"
log "       dashboard published"

# ---------------------------------------------------------------------------
# 4. CloudTrail trail with KMS+S3 data events.
# ---------------------------------------------------------------------------
log "[4/4] CloudTrail ${CLOUDTRAIL_NAME}"

TRAIL_BUCKET="${RAW_BUCKET}-cloudtrail"
log "       trail bucket ${TRAIL_BUCKET}"
if aws s3api head-bucket --bucket "${TRAIL_BUCKET}" 2>/dev/null; then
  log "       trail bucket already exists"
else
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${TRAIL_BUCKET}" --region "${REGION}"
  else
    aws s3api create-bucket --bucket "${TRAIL_BUCKET}" --region "${REGION}" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi
  aws s3api put-public-access-block \
    --bucket "${TRAIL_BUCKET}" \
    --public-access-block-configuration \
      "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
fi

# CloudTrail bucket policy
TMP_TPOL="$(mktemp)"
cat > "${TMP_TPOL}" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AWSCloudTrailAclCheck",
      "Effect": "Allow",
      "Principal": {"Service": "cloudtrail.amazonaws.com"},
      "Action": "s3:GetBucketAcl",
      "Resource": "arn:aws:s3:::${TRAIL_BUCKET}",
      "Condition": {"StringEquals": {"AWS:SourceArn": "arn:aws:cloudtrail:${REGION}:${ACCOUNT_ID}:trail/${CLOUDTRAIL_NAME}"}}
    },
    {
      "Sid": "AWSCloudTrailWrite",
      "Effect": "Allow",
      "Principal": {"Service": "cloudtrail.amazonaws.com"},
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::${TRAIL_BUCKET}/AWSLogs/${ACCOUNT_ID}/*",
      "Condition": {
        "StringEquals": {
          "s3:x-amz-acl": "bucket-owner-full-control",
          "AWS:SourceArn": "arn:aws:cloudtrail:${REGION}:${ACCOUNT_ID}:trail/${CLOUDTRAIL_NAME}"
        }
      }
    }
  ]
}
JSON
aws s3api put-bucket-policy --bucket "${TRAIL_BUCKET}" --policy "file://${TMP_TPOL}"
rm -f "${TMP_TPOL}"

if aws cloudtrail describe-trails --trail-name-list "${CLOUDTRAIL_NAME}" \
      --region "${REGION}" --query 'trailList[0].Name' --output text 2>/dev/null \
      | grep -q "${CLOUDTRAIL_NAME}"; then
  log "       trail already exists — updating"
  aws cloudtrail update-trail \
    --name "${CLOUDTRAIL_NAME}" \
    --s3-bucket-name "${TRAIL_BUCKET}" \
    --include-global-service-events \
    --is-multi-region-trail \
    --region "${REGION}" >/dev/null
else
  aws cloudtrail create-trail \
    --name "${CLOUDTRAIL_NAME}" \
    --s3-bucket-name "${TRAIL_BUCKET}" \
    --include-global-service-events \
    --is-multi-region-trail \
    --region "${REGION}" >/dev/null
  log "       trail created"
fi

# Advanced event selectors:
#   - One Data-event selector for S3 objects under the raw bucket. KMS
#     `AWS::KMS::Key` is not a valid data-event resource type — KMS API
#     calls (Encrypt / Decrypt / GenerateDataKey) are *management* events,
#     so we capture them via a Management selector instead. Both come
#     from the same trail; the audit dashboard in Phase 4 joins them.
TMP_AES="$(mktemp)"
cat > "${TMP_AES}" <<JSON
[
  {
    "Name": "Management-events",
    "FieldSelectors": [
      {"Field": "eventCategory", "Equals": ["Management"]}
    ]
  },
  {
    "Name": "PII-data-events-S3",
    "FieldSelectors": [
      {"Field": "eventCategory", "Equals": ["Data"]},
      {"Field": "resources.type", "Equals": ["AWS::S3::Object"]},
      {"Field": "resources.ARN",
       "StartsWith": ["arn:aws:s3:::${RAW_BUCKET}/"]
      }
    ]
  }
]
JSON
aws cloudtrail put-event-selectors \
  --trail-name "${CLOUDTRAIL_NAME}" \
  --advanced-event-selectors "file://${TMP_AES}" \
  --region "${REGION}" >/dev/null
rm -f "${TMP_AES}"

aws cloudtrail start-logging --name "${CLOUDTRAIL_NAME}" --region "${REGION}"
log "       trail logging enabled (management + S3 data events on ${RAW_BUCKET})"

cat >> /tmp/lending-phase1-outputs.env <<EOF
SNS_P1_ARN=${SNS_P1_ARN}
SNS_P2_ARN=${SNS_P2_ARN}
EOF

log "Done."
warn "Action required: confirm both SNS subscription emails in ${ALERT_EMAIL}."
