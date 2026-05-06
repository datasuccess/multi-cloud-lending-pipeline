# credit_bureau_pulls_generator

One synthetic bureau pull per `loan_applications` row. Reads the latest
`_SUCCESS`'d `loan_applications` partition; emits a row with FICO-shaped
`bureau_score`, score-correlated `delinquencies_count`, and `pulled_at`
1–30 min after the parent's `applied_at`.

## Local invocation

```bash
RAW_BUCKET_URI=/tmp/lending-raw \
python -c "from lambdas.credit_bureau_pulls_generator.handler import lambda_handler; \
           print(lambda_handler({'seed': 42}, None))"
```

Requires a customers + loan_applications partition under
`/tmp/lending-raw/raw/<source>/ingest_date=.../` first — bureau cannot
run as the root.

## Lambda invocation (after Phase 2 deploy)

```bash
aws lambda invoke \
  --function-name lending-bureau-generator \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  out.json && cat out.json
```

## Output layout

```
s3://lending-raw-<acct>/raw/credit_bureau_pulls/ingest_date=YYYY-MM-DD/
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet.manifest.json
└── _SUCCESS
```

## Realism contract

Documented in `docs/02-fan-out.md` §13.1, §13.2:

- `pulled_at` ∈ [applied_at + 1m, applied_at + 30m]
- `bureau_score` Beta(8, 4) scaled to [300, 850] — FICO-ish median ~700
- `delinquencies_count` ~ Poisson(λ) where λ scales inversely with score
- `hard_inquiry` 95% true (loan apps almost always trigger a hard pull)
- `bureau_name` ~ uniform(experian, equifax, transunion)

## Runbook

### Alarm: lending-bureau-errors

**What it means.** Lambda raised — usually `ParentNotFound` (loan_apps
partition missing for today) or post-write validation failure.

**First steps.**
1. Read the latest manifest sidecar — if validation_passed is False,
   `validation_errors` describes the failure.
2. If `ParentNotFound`: check `loan_applications` `_SUCCESS` for today's
   ingest_date; investigate why the loan_apps Lambda didn't write.
3. Replay with `{"ingest_date":"YYYY-MM-DD"}` once parent landed.

### Alarm: lending-bureau-low-volume

**What it means.** Wrote < 10 000 rows (prod) / < 300 rows (test).

**First steps.**
1. Check the parent loan_apps partition — bureau is 1:1, so a low
   bureau count almost always means low loan_apps.
2. Read the manifest for `rows`. If matches loan_apps row count,
   tune the alarm threshold; if not, investigate.

### Alarm: lending-bureau-freshness

**What it means.** No `heartbeat` metric for > 26 hours (prod) / > 12
hours (test).

**First steps.**
1. `aws events describe-rule --name lending-bureau-daily` — confirm
   `State=ENABLED`.
2. Check loan_apps freshness — bureau is downstream, so an upstream
   failure cascades.
3. Invoke manually with `{"trigger":"freshness-recovery"}`.
