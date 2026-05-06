# customer_generator

Root of the Phase 2 dependency graph: writes one `customers` Parquet
partition per `ingest_date`, with ~95% net-new customers and ~5%
returning customers sampled from prior partitions (with realistic
mutations to address, income, KYC, employment).

Downstream sources (`loan_applications` and below) sample
`customer_id`s from this partition for FK consistency.

## Local invocation

```bash
# from repo root, no AWS needed
RAW_BUCKET_URI=/tmp/lending-raw \
ROWS_PER_RUN=12000 \
python -c "from lambdas.customer_generator.handler import lambda_handler; \
           print(lambda_handler({'seed': 42}, None))"
```

First run writes 100% net-new (no parent partition yet). Re-run: the
returning share kicks in.

## Lambda invocation (after Phase 2 deploy)

```bash
aws lambda invoke \
  --function-name lending-customers-generator \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  out.json && cat out.json
```

Backfill a past partition (will look at the partition immediately before
`ingest_date` for returning samples):

```bash
aws lambda invoke \
  --function-name lending-customers-generator \
  --payload '{"ingest_date":"2026-04-25"}' \
  --cli-binary-format raw-in-base64-out \
  out.json
```

## Output layout

```
s3://lending-raw-<acct>/raw/customers/ingest_date=YYYY-MM-DD/
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet.manifest.json
└── _SUCCESS
```

## Realism contract

Documented in `docs/02-fan-out.md` §13.2:
- New: 70% pending KYC, mostly within last 30 days
- Returning: 96% verified KYC, 90% same address, income drifts ±5%
  (clamped at ±25%), 8% employment transitions
- `created_at` preserved across re-appearances; `updated_at` advances

## Runbook

### Alarm: lending-customers-errors

**What it means.** Lambda raised — usually post-write validation
failure (rare; we generate from a known-good schema) or a transient
S3/KMS issue.

**First steps.**
1. Read the latest manifest sidecar — `validation_passed` and
   `validation_errors` are the precise failure description.
2. Check CloudWatch logs for `run_id`.
3. If transient, replay with `{"ingest_date":"YYYY-MM-DD"}`.

### Alarm: lending-customers-low-volume

**What it means.** Wrote < 8 000 rows (prod) / < 300 rows (test).

**First steps.**
1. Verify `ROWS_PER_RUN` env var.
2. Read the manifest for `rows` and any validation errors.
3. Replay if config-driven; investigate if not.

### Alarm: lending-customers-freshness

**What it means.** No `heartbeat` metric for > 25 hours (prod) / > 12
hours (test).

**First steps.**
1. `aws events describe-rule --name lending-customers-daily` — confirm
   `State=ENABLED`.
2. Invoke manually with `{"trigger":"freshness-recovery"}`.
