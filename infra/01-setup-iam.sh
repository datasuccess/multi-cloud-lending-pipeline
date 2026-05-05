#!/usr/bin/env bash
# =============================================================================
# Phase 1 — IAM:
#   - lending-loan-app-generator-role     (Lambda execution; encrypt-only on KMS)
#   - lending-pii-loader-role             (created now, used Phase 4 by Snowflake/RS)
#   - lending-pii-investigator-role       (MFA-gated, 4h max session, audited unmask)
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
  die "Run 00-setup-foundations.sh first — /tmp/lending-phase1-outputs.env missing."
fi

LAMBDA_TRUST='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

# Loader trust: same-account principals can assume — Snowflake/RS swap in their own
# IAM-user ARN + external ID via STORAGE INTEGRATION when Phase 4 lands.
LOADER_TRUST="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::${ACCOUNT_ID}:root"},
    "Action": "sts:AssumeRole"
  }]
}
JSON
)"

# Investigator trust: same account + MFA REQUIRED. Phase 4 narrows the principal
# to specific user ARNs and adds an aws:RequestTag time-bound condition.
INVESTIGATOR_TRUST="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::${ACCOUNT_ID}:root"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "Bool": {"aws:MultiFactorAuthPresent": "true"}
    }
  }]
}
JSON
)"

ensure_role() {
  local role="$1" trust="$2" max_session="${3:-3600}"
  if aws iam get-role --role-name "${role}" >/dev/null 2>&1; then
    log "       role ${role} already exists"
    aws iam update-assume-role-policy --role-name "${role}" \
      --policy-document "${trust}"
  else
    aws iam create-role --role-name "${role}" \
      --assume-role-policy-document "${trust}" \
      --max-session-duration "${max_session}" \
      --tags Key=Project,Value=lending Key=Phase,Value=1 >/dev/null
    log "       created ${role}"
  fi
}

# ---------------------------------------------------------------------------
# 1. Generator Lambda role.
# ---------------------------------------------------------------------------
log "[1/3] ${LAMBDA_ROLE}"
ensure_role "${LAMBDA_ROLE}" "${LAMBDA_TRUST}" 3600

aws iam attach-role-policy \
  --role-name "${LAMBDA_ROLE}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

GEN_POLICY="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WriteRawLoanApplicationsPrefix",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::${RAW_BUCKET}/raw/loan_applications/*"
    },
    {
      "Sid": "WritePipelineRunsLedger",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::${RAW_BUCKET}/_pipeline_runs/source=loan_applications/*"
    },
    {
      "Sid": "EncryptOnly",
      "Effect": "Allow",
      "Action": ["kms:GenerateDataKey", "kms:Encrypt"],
      "Resource": "${KMS_KEY_ARN}"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents", "logs:CreateLogGroup"],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${LAMBDA_NAME}*"
    }
  ]
}
JSON
)"
aws iam put-role-policy \
  --role-name "${LAMBDA_ROLE}" \
  --policy-name "${LAMBDA_INLINE_POLICY}" \
  --policy-document "${GEN_POLICY}"
log "       inline policy ${LAMBDA_INLINE_POLICY} applied"

# Note: the validation read-back step needs s3:GetObject to confirm the file
# round-trips. Add it to the loan_applications prefix.
GET_POLICY="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "ReadBackOwnObjects",
    "Effect": "Allow",
    "Action": ["s3:GetObject"],
    "Resource": "arn:aws:s3:::${RAW_BUCKET}/raw/loan_applications/*"
  },
  {
    "Sid": "DecryptOwnObjects",
    "Effect": "Allow",
    "Action": ["kms:Decrypt"],
    "Resource": "${KMS_KEY_ARN}"
  }]
}
JSON
)"
aws iam put-role-policy \
  --role-name "${LAMBDA_ROLE}" \
  --policy-name "${LAMBDA_INLINE_POLICY}-readback" \
  --policy-document "${GET_POLICY}"
log "       read-back inline policy applied (post-write validation needs GetObject + KMS Decrypt)"

# ---------------------------------------------------------------------------
# 2. PII loader role (used Phase 4).
# ---------------------------------------------------------------------------
log "[2/3] ${PII_LOADER_ROLE}"
ensure_role "${PII_LOADER_ROLE}" "${LOADER_TRUST}" 3600

LOADER_POLICY="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadRaw",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::${RAW_BUCKET}",
        "arn:aws:s3:::${RAW_BUCKET}/*"
      ]
    },
    {
      "Sid": "DecryptPII",
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:DescribeKey"],
      "Resource": "${KMS_KEY_ARN}"
    }
  ]
}
JSON
)"
aws iam put-role-policy \
  --role-name "${PII_LOADER_ROLE}" \
  --policy-name "lending-pii-loader-policy" \
  --policy-document "${LOADER_POLICY}"
log "       loader inline policy applied"

# ---------------------------------------------------------------------------
# 3. PII investigator role — MFA + 4h MAX session.
# ---------------------------------------------------------------------------
log "[3/3] ${PII_INVESTIGATOR_ROLE}"
ensure_role "${PII_INVESTIGATOR_ROLE}" "${INVESTIGATOR_TRUST}" 14400

INVESTIGATOR_POLICY="${LOADER_POLICY}"  # same read+decrypt scope, different trust
aws iam put-role-policy \
  --role-name "${PII_INVESTIGATOR_ROLE}" \
  --policy-name "lending-pii-investigator-policy" \
  --policy-document "${INVESTIGATOR_POLICY}"
log "       investigator inline policy applied (MFA-required trust, 4h max session)"

cat >> /tmp/lending-phase1-outputs.env <<EOF
LAMBDA_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE}
PII_LOADER_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${PII_LOADER_ROLE}
PII_INVESTIGATOR_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${PII_INVESTIGATOR_ROLE}
EOF

log "Done."
log "Sleep 10s so IAM propagates before Lambda create-function reads the role..."
sleep 10
