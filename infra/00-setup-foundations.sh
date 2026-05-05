#!/usr/bin/env bash
# =============================================================================
# Phase 1 — foundations: KMS key + alias, S3 bucket (SSE-KMS, bucket-key,
# public-access-block, deny-insecure policy), AWS Budget alarm.
# Idempotent — safe to re-run.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/_env.sh"

log "Account=${ACCOUNT_ID} Region=${REGION}"

# ---------------------------------------------------------------------------
# 1. AWS Budget — set FIRST so we have a tripwire before anything else runs.
# ---------------------------------------------------------------------------
log "[1/4] AWS Budget '${BUDGET_NAME}' at \$${BUDGET_LIMIT_USD}/mo"
TMP_BUDGET="$(mktemp)"
TMP_NOTIF="$(mktemp)"
cat > "${TMP_BUDGET}" <<JSON
{
  "BudgetName": "${BUDGET_NAME}",
  "BudgetLimit": {"Amount": "${BUDGET_LIMIT_USD}", "Unit": "USD"},
  "TimeUnit": "MONTHLY",
  "BudgetType": "COST",
  "CostFilters": {"TagKeyValue": ["user:Project\$lending"]}
}
JSON
cat > "${TMP_NOTIF}" <<JSON
[
  {
    "Notification": {
      "NotificationType": "ACTUAL",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 80,
      "ThresholdType": "PERCENTAGE",
      "NotificationState": "ALARM"
    },
    "Subscribers": [
      {"SubscriptionType": "EMAIL", "Address": "${ALERT_EMAIL}"}
    ]
  },
  {
    "Notification": {
      "NotificationType": "FORECASTED",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 100,
      "ThresholdType": "PERCENTAGE",
      "NotificationState": "ALARM"
    },
    "Subscribers": [
      {"SubscriptionType": "EMAIL", "Address": "${ALERT_EMAIL}"}
    ]
  }
]
JSON
if aws budgets describe-budget \
      --account-id "${ACCOUNT_ID}" --budget-name "${BUDGET_NAME}" >/dev/null 2>&1; then
  log "       budget already exists — skipping create"
else
  aws budgets create-budget \
    --account-id "${ACCOUNT_ID}" \
    --budget "file://${TMP_BUDGET}" \
    --notifications-with-subscribers "file://${TMP_NOTIF}"
  log "       created"
fi
rm -f "${TMP_BUDGET}" "${TMP_NOTIF}"

# ---------------------------------------------------------------------------
# 2. KMS key + alias.
# ---------------------------------------------------------------------------
log "[2/4] KMS key + alias ${KMS_ALIAS}"
KMS_KEY_ID="$(aws kms describe-key --key-id "${KMS_ALIAS}" --region "${REGION}" \
  --query 'KeyMetadata.KeyId' --output text 2>/dev/null || true)"

if [[ -z "${KMS_KEY_ID}" || "${KMS_KEY_ID}" == "None" ]]; then
  KMS_KEY_ID="$(aws kms create-key \
    --description "Lending project PII envelope key" \
    --key-usage ENCRYPT_DECRYPT \
    --tags TagKey=Project,TagValue=lending TagKey=Phase,TagValue=1 \
    --region "${REGION}" \
    --query 'KeyMetadata.KeyId' --output text)"
  aws kms enable-key-rotation --key-id "${KMS_KEY_ID}" --region "${REGION}"
  aws kms create-alias \
    --alias-name "${KMS_ALIAS}" \
    --target-key-id "${KMS_KEY_ID}" \
    --region "${REGION}"
  log "       created KeyId=${KMS_KEY_ID}"
else
  log "       already exists KeyId=${KMS_KEY_ID}"
fi
KMS_KEY_ARN="arn:aws:kms:${REGION}:${ACCOUNT_ID}:key/${KMS_KEY_ID}"

# ---------------------------------------------------------------------------
# 3. S3 bucket: create, block public access, SSE-KMS + bucket-key, deny-insecure.
# ---------------------------------------------------------------------------
log "[3/4] S3 bucket ${RAW_BUCKET}"
if aws s3api head-bucket --bucket "${RAW_BUCKET}" 2>/dev/null; then
  log "       bucket already exists"
else
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${RAW_BUCKET}" --region "${REGION}"
  else
    aws s3api create-bucket --bucket "${RAW_BUCKET}" --region "${REGION}" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi
  log "       created"
fi

aws s3api put-public-access-block \
  --bucket "${RAW_BUCKET}" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-encryption \
  --bucket "${RAW_BUCKET}" \
  --server-side-encryption-configuration "{
    \"Rules\": [{
      \"ApplyServerSideEncryptionByDefault\": {
        \"SSEAlgorithm\": \"aws:kms\",
        \"KMSMasterKeyID\": \"${KMS_KEY_ARN}\"
      },
      \"BucketKeyEnabled\": true
    }]
  }"

TMP_POLICY="$(mktemp)"
cat > "${TMP_POLICY}" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::${RAW_BUCKET}",
        "arn:aws:s3:::${RAW_BUCKET}/*"
      ],
      "Condition": {"Bool": {"aws:SecureTransport": "false"}}
    },
    {
      "Sid": "DenyUnEncryptedObjectUploads",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::${RAW_BUCKET}/*",
      "Condition": {
        "StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"}
      }
    }
  ]
}
JSON
aws s3api put-bucket-policy --bucket "${RAW_BUCKET}" --policy "file://${TMP_POLICY}"
rm -f "${TMP_POLICY}"

# Wrap TAGS_JSON in a TagSet object — CLI rejects the `TagSet=<json>` shorthand.
aws s3api put-bucket-tagging --bucket "${RAW_BUCKET}" \
  --tagging "{\"TagSet\": ${TAGS_JSON}}"

log "       SSE-KMS + bucket-key + PAB + deny-insecure policy applied"

# ---------------------------------------------------------------------------
# 4. Capture outputs for the downstream scripts.
# ---------------------------------------------------------------------------
log "[4/4] Writing /tmp/lending-phase1-outputs.env"
cat > /tmp/lending-phase1-outputs.env <<EOF
KMS_KEY_ID=${KMS_KEY_ID}
KMS_KEY_ARN=${KMS_KEY_ARN}
RAW_BUCKET=${RAW_BUCKET}
RAW_BUCKET_URI=${RAW_BUCKET_URI}
EOF
log "Done. KMS=${KMS_KEY_ARN}"
