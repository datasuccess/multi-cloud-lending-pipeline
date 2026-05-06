# RUNBOOK — multi-cloud-lending-pipeline

The single page you open to run this project end-to-end. Each phase appends to the same file.

> AWS account: `497162053528` · Region: `us-east-1` · Alerts: `datasuccess1@gmail.com`

## Conventions

- All commands run from the repo root.
- Scripts are idempotent — safe to re-run.
- Do not run any teardown except deliberately (`./infra/99-teardown.sh`).

---

## Phase 1 — loan_applications generator

### 0. One-time local setup

```bash
# Python 3.11+ venv with pyarrow + Faker + Powertools + boto3 + streamlit deps.
python3.11 -m venv .venv         # or 3.13 — pyarrow 18+ wheels exist for both
source .venv/bin/activate
pip install -U pip setuptools wheel
pip install -e ".[dev,streamlit]"

# Sanity: tests should pass without any AWS access.
pytest -q

# AWS creds + region.
aws sts get-caller-identity      # confirm 497162053528
aws configure get region         # confirm us-east-1
chmod +x infra/*.sh infra/lambda/*.sh
```

### 1. Provision AWS (in order)

```bash
./infra/00-setup-foundations.sh         # Budget, KMS, S3 raw bucket
./infra/01-setup-iam.sh                 # 3 IAM roles
./infra/02-setup-monitoring.sh          # SNS, alarms, dashboard, CloudTrail
                                        #   (CONFIRM both SNS subscription emails)
./infra/lambda/build-layer.sh           # pyarrow + faker + powertools layer
./infra/lambda/package-function.sh      # Code zip
./infra/lambda/deploy.sh                # Lambda + EventBridge daily cron (prod-shaped)
```

### 2. Smoke test + chaos verification

```bash
./infra/03-invoke-and-backfill.sh smoke      # one manual invocation
./infra/03-invoke-and-backfill.sh verify     # list partitions + tail manifest
./infra/04-chaos-test.sh                     # forces validation failure
# Wait ~5 minutes, confirm alarm:
aws cloudwatch describe-alarms --alarm-names lending-loan-app-errors \
  --region us-east-1 --query 'MetricAlarms[0].StateValue'
# After verifying, clean the chaos partition (commands printed by the script).
```

### 3. Bootstrap Secrets Manager + flip to test mode

```bash
./infra/05-bootstrap-secrets.sh         # creates lending/dev/streamlit-config
./infra/06-set-mode.sh test             # 6-hourly cron + anomaly engine on
                                        # (low-volume threshold drops to 400)
```

In test mode the pipeline runs every 6 hours (00, 06, 12, 18 UTC) and rolls a die:
- 3% SKIP → exercises the freshness alarm
- 10% UNDERSHOOT → exercises the low-volume alarm
- 5% SILENT_FAIL → exercises the errors alarm
- 5% SLOW → exercises the duration widget

See [`docs/anomaly-injection.md`](docs/anomaly-injection.md).

### 4. Watch it work — Streamlit monitoring app

```bash
streamlit run streamlit_app/Home.py
```

Pages:
- **Home** — alarm states, latest partitions, runs ledger
- **🩺 Pipeline Health** — per-day success/failure, duration trends
- **📊 Lending KPIs** — volume / amount / DTI / channel / state mix

Offline mode (no AWS round-trips): see [`streamlit_app/README.md`](streamlit_app/README.md).

### 5. Backfill (so dbt has history in Phase 5)

```bash
./infra/03-invoke-and-backfill.sh backfill 14   # 14 days
```

### 6. Switch back to prod when you're done observing chaos

```bash
./infra/06-set-mode.sh prod             # daily 03:00 UTC, anomaly engine off
```

### 7. Teardown (when finished with Phase 1)

```bash
./infra/99-teardown.sh
```

KMS key isn't auto-deleted (AWS forces a 7-30 day pending window). The script prints the one extra command if you want it gone.

---

## Phase 2 — fan-out generators

Six new sources land on the same Lambda + S3 + EventBridge pattern Phase 1
established. Wiring is **additive**: existing Phase 1 resources stay
untouched.

```
   customers ─┐
              ├── loan_applications ── credit_bureau_pulls
              │                    ├── loan_decisions ── loan_drawdowns
              │                    │                  ├── payments
              │                    │                  └── delinquencies
              └────────────────────┘
```

### 1. Provision (in order, after Phase 1 is healthy)

```bash
./infra/01-setup-iam-fanout.sh           # shared lending-fanout-generator-role
./infra/02-setup-monitoring-fanout.sh    # 6 DLQs + 24 alarms + 12 dashboard widgets
./infra/lambda/package-fanout.sh         # one zip per source under build/
./infra/lambda/02-deploy-fanout.sh       # 6 Lambdas + EventBridge rules (prod offsets)
```

Schedules per `docs/02-fan-out.md` §5:

| Time (UTC) | Source        |
|------------|---------------|
| 02:50      | customers     |
| 03:00      | loan_applications (Phase 1) |
| 03:10      | credit_bureau_pulls |
| 03:15      | loan_decisions |
| 03:30      | loan_drawdowns |
| 03:45      | payments      |
| 04:00      | delinquencies |

### 2. Smoke chain (manual, in order)

```bash
for fn in lending-customers-generator lending-bureau-pulls-generator \
          lending-decisions-generator lending-drawdowns-generator \
          lending-payments-generator lending-delinquencies-generator; do
  aws lambda invoke --function-name "$fn" --region us-east-1 \
    --cli-binary-format raw-in-base64-out \
    --payload '{"trigger":"manual-smoke"}' "/tmp/out-$fn.json"
  cat "/tmp/out-$fn.json"
  echo
done
```

Each downstream Lambda raises `ParentNotFound` if its parent partition
hasn't landed yet — fix by re-running upstream first.

### 3. Flip to test mode (anomaly engine + 6-hourly)

```bash
./infra/06-set-mode-fanout.sh test
```

Test-mode thresholds match the Phase 1 pattern:
- MIN_ROWS lowered per source (400 / 200 / 50 — see `_env.sh`).
- low-volume alarm thresholds re-tuned to the same floor.
- freshness window 12h (two missed runs in a row).

Switch back with `./infra/06-set-mode-fanout.sh prod`.

### 4. DLQ inspection

If an `lending-<source>-dlq-depth` alarm fires:

```bash
QUEUE_URL=$(aws sqs get-queue-url --queue-name lending-<source>-dlq \
  --region us-east-1 --query 'QueueUrl' --output text)
aws sqs receive-message --queue-url "$QUEUE_URL" --max-number-of-messages 5 \
  --wait-time-seconds 1 --region us-east-1
```

The body is the EventBridge invocation payload. After diagnosing, drain
with `aws sqs purge-queue --queue-url "$QUEUE_URL"`.

### 5. End-to-end verification

```bash
.venv/bin/python -m pytest tests/test_fan_out_e2e.py -q
```

Asserts the cross-source FK graph is intact + temporal invariants
(applied_at ≤ pulled_at ≤ decided_at on the same application).

---

## Phase 3+ — landed when the phases land

Each subsequent phase appends a section here.
