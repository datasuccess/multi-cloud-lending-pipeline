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

# ---------------------------------------------------------------------------
# Phase 2 — fan-out source registry.
#
# Six new sources, each with its own Lambda + EventBridge rule + DLQ + alarms.
# Parallel arrays, indexed identically. Helpers below derive resource names so
# every script reads from the same source of truth.
#
# Source order is intentionally the dependency order — customers first,
# delinquencies last. Schedules in §5 reflect this.
# ---------------------------------------------------------------------------

export PHASE2_SOURCES=(customers credit_bureau_pulls loan_decisions loan_drawdowns payments delinquencies)

# Generator package directory under lambdas/. NOTE: source plurality differs
# (e.g., "loan_decisions" the source vs. "loan_decision_generator" the package).
export PHASE2_PACKAGES=(customer_generator credit_bureau_pulls_generator loan_decision_generator loan_drawdown_generator payment_generator delinquency_generator)

# Powertools service names — must match the literals in each handler.py
# (`get_metrics("<service>")`). These are the `service` dimension on EMF metrics.
export PHASE2_SERVICES=(customers-generator bureau-generator decisions-generator drawdowns-generator payments-generator delinquencies-generator)

# Lambda function name suffix (full name = "lending-<short>-generator").
export PHASE2_SHORT_NAMES=(customers bureau-pulls decisions drawdowns payments delinquencies)

# Prod EventBridge schedules (UTC). Offsets per docs/02-fan-out.md §5.
export PHASE2_PROD_CRONS=(
  "cron(50 2 * * ? *)"   # customers     02:50
  "cron(10 3 * * ? *)"   # bureau        03:10
  "cron(15 3 * * ? *)"   # decisions     03:15
  "cron(30 3 * * ? *)"   # drawdowns     03:30
  "cron(45 3 * * ? *)"   # payments      03:45
  "cron(0 4 * * ? *)"    # delinquencies 04:00
)

# Test mode: 4× / day, offsets shifted to fire after Phase 1 loan_apps
# (which fires at 00, 06, 12, 18 UTC in test). Customers fires *before* loan_apps.
export PHASE2_TEST_CRONS=(
  "cron(50 5,11,17,23 * * ? *)"   # customers     :50 before each loan_apps slot
  "cron(10 0,6,12,18 * * ? *)"    # bureau        :10 after
  "cron(15 0,6,12,18 * * ? *)"    # decisions     :15 after
  "cron(30 0,6,12,18 * * ? *)"    # drawdowns     :30 after
  "cron(45 0,6,12,18 * * ? *)"    # payments      :45 after
  "cron(0 1,7,13,19 * * ? *)"     # delinquencies :00 of next hour
)

# Per-source low-volume thresholds. Prod = realistic floor; test = matches
# undershoot range so chaos undershoots surface as low-volume not errors.
# Per docs/02-fan-out.md §7.
export PHASE2_PROD_MIN_ROWS=(10000 10000 10000 6000 20000 1000)
export PHASE2_TEST_MIN_ROWS=(400 400 400 200 200 50)

# Per-source rows_per_run (prod). Test mode shrinks all of these.
# Note: bureau/decisions/drawdowns/payments/delinquencies are derived from
# parents — no rows_per_run knob; only customers + loan_apps have one.
export PHASE2_PROD_ROWS_PER_RUN=(12000 0 0 0 0 0)
export PHASE2_TEST_ROWS_PER_RUN=(2000 0 0 0 0 0)

# Resource-name accessors (parallel-indexed from PHASE2_SHORT_NAMES).
phase2_lambda_name()   { echo "lending-${PHASE2_SHORT_NAMES[$1]}-generator"; }
phase2_role_name()     { echo "lending-fanout-generator-role"; }  # one shared role
phase2_rule_name()     { echo "lending-${PHASE2_SHORT_NAMES[$1]}-daily"; }
phase2_dlq_name()      { echo "lending-${PHASE2_SHORT_NAMES[$1]}-dlq"; }
phase2_alarm_errors()  { echo "lending-${PHASE2_SHORT_NAMES[$1]}-errors"; }
phase2_alarm_fresh()   { echo "lending-${PHASE2_SHORT_NAMES[$1]}-freshness"; }
phase2_alarm_lowvol()  { echo "lending-${PHASE2_SHORT_NAMES[$1]}-low-volume"; }
phase2_alarm_dlq()     { echo "lending-${PHASE2_SHORT_NAMES[$1]}-dlq-depth"; }
phase2_handler_path()  { echo "lambdas.${PHASE2_PACKAGES[$1]}.handler.lambda_handler"; }
