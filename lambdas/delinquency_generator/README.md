# delinquency_generator

Daily snapshot of late drawdowns. **Derived, not invented** — reads
drawdowns + the full payments history through `as_of_date`, computes
the per-drawdown cumulative scheduled-vs-actual gap, emits one row per
drawdown where the gap is positive.

## Local invocation

```bash
RAW_BUCKET_URI=/tmp/lending-raw \
python -c "from lambdas.delinquency_generator.handler import lambda_handler; \
           print(lambda_handler({}, None))"
```

Requires the full upstream chain plus *at least one* payments partition
through today. No `seed` knob — the snapshot is a deterministic function
of upstream data.

## Lambda invocation (after Phase 2 deploy)

```bash
aws lambda invoke \
  --function-name lending-delinquencies-generator \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  out.json && cat out.json
```

## Output layout

```
s3://lending-raw-<acct>/raw/delinquencies/ingest_date=YYYY-MM-DD/
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet.manifest.json
└── _SUCCESS
```

## Realism contract (docs/02-fan-out.md §13.6)

DPD bucketing:

```
1–30   → "1-30"
31–60  → "31-60"
61–90  → "61-90"
> 90   → "90+"
```

Anchor: the earliest scheduled_at where the running cumulative gap
turned positive **and stayed positive** through the latest payment.
`dpd_days = (as_of_date - anchor).days`, floored at 1.

`outstanding_principal = drawn_amount - sum(principal_amount across
history)`, floored at 0. This is approximate (no compounding); Phase 5
dbt is the production-style enforcement layer.

## Runbook

### Alarm: lending-delinquencies-errors

**What it means.** ParentNotFound (drawdowns or payments missing) or
validation failure.

**First steps.**
1. Read manifest sidecar.
2. Both drawdowns *and* payments must have at least one `_SUCCESS`'d
   partition through today. If the very first day has no prior
   payments, this generator can't run yet — that's expected on day 0.

### Alarm: lending-delinquencies-low-volume

**What it means.** Wrote < 100 rows.

**First steps.**
1. Low delinquency volume can be legitimate (synthetic data, early
   days, or strong cohort). Cross-check the payments cohort — if the
   miss rate is reasonable but cumulative gaps haven't accrued yet,
   that's day 1–2 behavior.
2. After ~5+ payment cycles, expected late portfolio is ~5% of
   drawdowns. Anything well below that is suspicious.

### Alarm: lending-delinquencies-freshness

**What it means.** No `heartbeat` for > 26h (prod) / > 12h (test).

**First steps.**
1. Confirm schedule (`aws events describe-rule
   --name lending-delinquencies-daily`).
2. Walk upstream — payments must run before delinquencies.
