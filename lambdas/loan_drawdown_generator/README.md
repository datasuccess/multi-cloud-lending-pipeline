# loan_drawdown_generator

One drawdown per approved loan_decisions row. Reads the latest
`_SUCCESS`'d decisions partition, filters to `decision='approved'`,
emits drawn_amount (full or partial) plus the masked account_last4
and disbursed_at delay.

We denormalize `approved_amount`, `apr_pct`, `term_months` from the
decision so payments + delinquencies don't have to re-join all the way
back.

## Local invocation

```bash
RAW_BUCKET_URI=/tmp/lending-raw \
python -c "from lambdas.loan_drawdown_generator.handler import lambda_handler; \
           print(lambda_handler({'seed': 42}, None))"
```

Requires customers + loan_apps + bureau + decisions partitions for
today first.

## Lambda invocation (after Phase 2 deploy)

```bash
aws lambda invoke \
  --function-name lending-drawdowns-generator \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  out.json && cat out.json
```

## Output layout

```
s3://lending-raw-<acct>/raw/loan_drawdowns/ingest_date=YYYY-MM-DD/
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet.manifest.json
└── _SUCCESS
```

## Realism contract (docs/02-fan-out.md §13.4)

- 70% of approved customers draw the **full** approved amount
- 30% draw partial: uniform between 30% and 99% of approved
- `account_last4` = 4-digit zero-padded random
- `disbursed_at` = `decided_at` + log-normal delay (median ~4.5h),
  capped at 48h

## Runbook

### Alarm: lending-drawdowns-errors

**What it means.** ParentNotFound (decisions missing for today), or
validation failure.

**First steps.**
1. Read latest manifest sidecar.
2. If ParentNotFound: check `loan_decisions/_SUCCESS` for today;
   replay missing parent first.

### Alarm: lending-drawdowns-low-volume

**What it means.** Wrote < 6 000 rows (prod) / < 200 rows (test).
Drawdowns is ~75% of decisions, so a low count usually means a low
upstream count or a sudden drop in approve rate.

**First steps.**
1. Check decisions partition row count + approve rate.
2. If both look normal but drawdowns is low: investigate filter logic.

### Alarm: lending-drawdowns-freshness

**What it means.** No `heartbeat` for > 26h (prod) / > 12h (test).

**First steps.**
1. Confirm schedule (`aws events describe-rule
   --name lending-drawdowns-daily`).
2. Walk upstream — decisions has to be fresh first.
