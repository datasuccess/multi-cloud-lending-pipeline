# payment_generator

One synthetic payment per active drawdown, per generator run. Reads
the latest drawdowns partition (FK parent) and the prior payments
partition (Markov state). Drives Phase 5 dbt's delinquency staging.

## Local invocation

```bash
RAW_BUCKET_URI=/tmp/lending-raw \
python -c "from lambdas.payment_generator.handler import lambda_handler; \
           print(lambda_handler({'seed': 42}, None))"
```

Requires the full upstream chain (customers → apps → bureau → decisions
→ drawdowns) to have landed for today. Prior payments partition is
optional — first run treats every drawdown as starting from the
favorable "paid_full" state.

## Lambda invocation (after Phase 2 deploy)

```bash
aws lambda invoke \
  --function-name lending-payments-generator \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  out.json && cat out.json
```

## Output layout

```
s3://lending-raw-<acct>/raw/payments/ingest_date=YYYY-MM-DD/
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet.manifest.json
└── _SUCCESS
```

## Realism contract (docs/02-fan-out.md §13.5)

State-dependent transition matrix:

| Prior status      | paid_full | paid_partial | missed |
|-------------------|-----------|--------------|--------|
| paid_full / new   | 92%       | 5%           | 3%     |
| paid_partial      | 60%       | 25%          | 15%    |
| missed            | 35%       | 25%          | 40%    |

Plus a tiny waiver rate (~0.1%) regardless of prior state.

Amortization: scheduled = standard fixed-payment formula. Interest
share approximated as `principal × APR / 12` (first-period rate, held
constant across the life of the loan for synthetic-data simplicity).

## Cap

Hard cap at 30 000 rows per run. When drawdowns > 30 000, the cap
truncates by `drawdown_id` ascending — same drawdowns each run, so
Markov continuity is preserved.

## Runbook

### Alarm: lending-payments-errors

**What it means.** ParentNotFound (drawdowns missing for today) or
validation failure.

**First steps.**
1. Read manifest sidecar.
2. Walk upstream — drawdowns has to be fresh first.

### Alarm: lending-payments-low-volume

**What it means.** Wrote < 20 000 rows (prod) / < 700 rows (test).

**First steps.**
1. Drawdowns is the parent. If it's low, payments will be low.
2. If prior payments partition is missing, every drawdown started fresh
   — that's still 1:1 with drawdowns, not low.

### Alarm: lending-payments-freshness

**What it means.** No `heartbeat` for > 26h (prod) / > 12h (test).

**First steps.**
1. Confirm schedule (`aws events describe-rule
   --name lending-payments-daily`).
2. Walk upstream.
