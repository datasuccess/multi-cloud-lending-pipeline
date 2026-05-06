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

## Phase 2+ — landed when the phases land

Each subsequent phase appends a section here.
