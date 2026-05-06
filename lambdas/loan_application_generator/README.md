# loan_application_generator

Daily synthetic-data Lambda. Writes one Parquet file per `ingest_date` partition,
plus a manifest sidecar and a `_SUCCESS` marker, plus a JSONL line in the
pipeline runs ledger.

## Local invocation

```bash
# from repo root
RAW_BUCKET_URI=/tmp/lending-raw \
ROWS_PER_RUN=12000 \
python -c "from lambdas.loan_application_generator.handler import lambda_handler; \
           print(lambda_handler({'seed': 42}, None))"
```

## Lambda invocation (after Phase 1 step 9)

```bash
aws lambda invoke \
  --function-name lending-loan-app-generator \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  out.json && cat out.json
```

Backfill a past partition:

```bash
aws lambda invoke \
  --function-name lending-loan-app-generator \
  --payload '{"ingest_date":"2026-04-25"}' \
  --cli-binary-format raw-in-base64-out \
  out.json
```

## Output layout

```
s3://lending-raw-<acct>/raw/loan_applications/ingest_date=YYYY-MM-DD/
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet.manifest.json
└── _SUCCESS
s3://lending-raw-<acct>/_pipeline_runs/source=loan_applications/year=YYYY/month=MM/day=DD/
└── run-<run_id>.jsonl
```

`_SUCCESS` is written **last and only on success**. Validation failures raise
before the marker is written, so downstream loaders never see a half-baked
partition.

## Runbook

### Alarm: lending-loan-app-errors

**What it means.** The Lambda raised in the last 5 min — most likely a
validation failure (post-write read-back asserts shape).

**First steps.**
1. Open the latest run in the runs ledger:
   `aws s3 ls s3://lending-raw-<acct>/_pipeline_runs/source=loan_applications/year=YYYY/month=MM/day=DD/`
2. Read the manifest sidecar; `validation_passed` and `validation_errors`
   tell you exactly what failed.
3. Check CloudWatch logs for `run_id` — Powertools tags every line with it.
4. If transient, replay:
   `aws lambda invoke --payload '{"ingest_date":"YYYY-MM-DD"}' …`.
5. If schema drift, bump `_generator_version` and follow the schema-change
   runbook (Phase 3 once Iceberg is in).

### Alarm: lending-loan-app-low-volume

**What it means.** Yesterday's run wrote `< 10,000` rows.

**Likely causes.**
1. `ROWS_PER_RUN` env var was changed.
2. Partial run — manifest will show `validation_passed=false`.
3. Generator regression — log-normal bounds rejected too many samples.

**First steps.**
1. Read the latest manifest: `rows`, `validation_errors`.
2. Inspect Lambda env: `aws lambda get-function-configuration …`.
3. If irrecoverable, delete the partition (parquet + manifest + `_SUCCESS`)
   and replay.

### Alarm: lending-loan-app-freshness

**What it means.** No `heartbeat` metric for > 25 hours. Either:
1. EventBridge didn't fire (check rule + target).
2. Lambda was throttled or disabled.
3. The whole metrics pipeline is broken (rare — check via test invocation).

**First steps.**
1. `aws events describe-rule --name lending-loan-app-daily` — verify
   `ScheduleExpression` and `State=ENABLED`.
2. `aws events list-targets-by-rule --rule lending-loan-app-daily` —
   confirm the Lambda ARN is the target.
3. Manually invoke once with `{"trigger":"freshness-recovery"}` to backfill.
