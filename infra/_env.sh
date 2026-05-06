#!/usr/bin/env bash
# Shared variables for every Phase 1 infra script.
# Source this file at the top of each script: `source "$(dirname "$0")/_env.sh"`

set -euo pipefail

export REGION="${REGION:-us-east-1}"
export ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

export PROJECT="lending"
export PHASE="1"

# Buckets / lakes
export RAW_BUCKET="lending-raw-${ACCOUNT_ID}"
export RAW_BUCKET_URI="s3://${RAW_BUCKET}"

# KMS
export KMS_ALIAS="alias/lending-pii"

# Lambda + role
export LAMBDA_NAME="lending-loan-app-generator"
export LAMBDA_ROLE="lending-loan-app-generator-role"
export LAMBDA_INLINE_POLICY="lending-loan-app-generator-policy"
export LAMBDA_LAYER_NAME="lending-pyarrow-layer"

# Phase-4 roles (created now, unused this phase)
export PII_LOADER_ROLE="lending-pii-loader-role"
export PII_INVESTIGATOR_ROLE="lending-pii-investigator-role"

# EventBridge
export EVENTBRIDGE_RULE="lending-loan-app-daily"

# SNS / alarms
export SNS_P1_TOPIC="lending-alerts-p1-page"
export SNS_P2_TOPIC="lending-alerts-p2-email"
export ALERT_EMAIL="${ALERT_EMAIL:-datasuccess1@gmail.com}"
export ALARM_ERRORS="lending-loan-app-errors"
export ALARM_FRESHNESS="lending-loan-app-freshness"
export ALARM_LOW_VOLUME="lending-loan-app-low-volume"

# Dashboard, CloudTrail
export DASHBOARD_NAME="lending-pipeline"
export CLOUDTRAIL_NAME="lending-pii-data-events"

# Budget
export BUDGET_NAME="lending-monthly-50usd"
export BUDGET_LIMIT_USD="50"

# Powertools
export POWERTOOLS_NAMESPACE="Lending/Generators"
export POWERTOOLS_SERVICE_NAME="loan-app-generator"

# Tags applied to taggable resources
export TAGS_JSON='[{"Key":"Project","Value":"lending"},{"Key":"Phase","Value":"1"},{"Key":"ManagedBy","Value":"manual-cli"},{"Key":"Owner","Value":"datasuccess"}]'

# Helpers
log()  { printf "\033[1;34m[%s]\033[0m %s\n" "$(date -u +%H:%M:%S)" "$*"; }
warn() { printf "\033[1;33m[%s]\033[0m %s\n" "$(date -u +%H:%M:%S)" "$*" >&2; }
die()  { printf "\033[1;31m[%s]\033[0m %s\n" "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }
