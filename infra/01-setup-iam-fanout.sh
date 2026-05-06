#!/usr/bin/env bash
# =============================================================================
# Phase 2 — IAM for the six fan-out generators.
#
# Creates one shared role `lending-fanout-generator-role`, used by all six
# Lambdas. Each Lambda needs:
#   - PutObject + GetObject on its own raw/<source>/* prefix.
#   - GetObject on every parent's raw/* prefix (for FK lookups).
#   - PutObject + GetObject on _pipeline_runs/source=<source>/* (ledger).
#   - KMS Encrypt + GenerateDataKey on the project key. Decrypt for read-back.
#   - SendMessage on the source's DLQ.
#   - Logs.
#
# Idempotent. Re-runs replace the inline policy in place.
# =============================================================================

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${HERE}/_env.sh"

[[ -f /tmp/lending-phase1-outputs.env ]] || die \
  "Run Phase 1 scripts first — /tmp/lending-phase1-outputs.env missing."
# shellcheck disable=SC1091
source /tmp/lending-phase1-outputs.env

[[ -n "${KMS_KEY_ARN:-}" ]] || die "KMS_KEY_ARN missing in outputs."

ROLE_NAME="$(phase2_role_name)"

LAMBDA_TRUST='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

log "[1/2] Role ${ROLE_NAME}"
if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  log "       already exists — refreshing trust policy"
  aws iam update-assume-role-policy --role-name "${ROLE_NAME}" \
    --policy-document "${LAMBDA_TRUST}"
else
  aws iam create-role --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "${LAMBDA_TRUST}" \
    --max-session-duration 3600 \
    --tags Key=Project,Value=lending Key=Phase,Value=2 >/dev/null
  log "       created"
fi

aws iam attach-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Build the resource lists — one S3 ARN per source for write, plus the
# parent-read pool. We grant Get across all `raw/*` because each downstream
# generator needs to read at least one parent prefix; narrowing further would
# need per-Lambda inline policies, which is more complexity than this scale
# warrants. The bucket is single-tenant.
DLQ_ARN_PATTERN="arn:aws:sqs:${REGION}:${ACCOUNT_ID}:lending-*-dlq"

POLICY_DOC="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WriteAllRawPrefixes",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::${RAW_BUCKET}/raw/*"
    },
    {
      "Sid": "ReadAllRawPrefixesForParentLookup",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::${RAW_BUCKET}/raw/*"
    },
    {
      "Sid": "WriteRunsLedger",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::${RAW_BUCKET}/_pipeline_runs/*"
    },
    {
      "Sid": "ListBucketForPartitionDiscovery",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::${RAW_BUCKET}",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["raw/*", "_pipeline_runs/*"]
        }
      }
    },
    {
      "Sid": "KMSEncrypt",
      "Effect": "Allow",
      "Action": ["kms:GenerateDataKey", "kms:Encrypt"],
      "Resource": "${KMS_KEY_ARN}"
    },
    {
      "Sid": "KMSDecryptForReadback",
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:DescribeKey"],
      "Resource": "${KMS_KEY_ARN}"
    },
    {
      "Sid": "DLQSend",
      "Effect": "Allow",
      "Action": ["sqs:SendMessage"],
      "Resource": "${DLQ_ARN_PATTERN}"
    }
  ]
}
JSON
)"

log "[2/2] Inline policy"
aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "lending-fanout-generator-policy" \
  --policy-document "${POLICY_DOC}"
log "       lending-fanout-generator-policy applied"

ROLE_ARN="$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)"
{
  echo "PHASE2_ROLE_ARN=${ROLE_ARN}"
} >> /tmp/lending-phase1-outputs.env
log "Role ARN: ${ROLE_ARN}"

# IAM propagation can take a few seconds before the first AssumeRole succeeds.
sleep 10
log "Done."
