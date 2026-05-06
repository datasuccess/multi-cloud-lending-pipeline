# loan_decision_generator

One decision per loan_applications row, produced by a small rule engine
that joins applications with bureau pulls. Output ranges from clean
approvals at low APR for super-prime borrowers to high_dti declines
that override score entirely.

## Local invocation

```bash
RAW_BUCKET_URI=/tmp/lending-raw \
python -c "from lambdas.loan_decision_generator.handler import lambda_handler; \
           print(lambda_handler({'seed': 42}, None))"
```

Requires `customers`, `loan_applications`, and `credit_bureau_pulls`
partitions for today first — this Lambda joins two parents.

## Lambda invocation (after Phase 2 deploy)

```bash
aws lambda invoke \
  --function-name lending-decisions-generator \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  out.json && cat out.json
```

## Output layout

```
s3://lending-raw-<acct>/raw/loan_decisions/ingest_date=YYYY-MM-DD/
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet
├── YYYY-MM-DDTHH-MM-SSZ_<run-id-prefix>.parquet.manifest.json
└── _SUCCESS
```

## Rule engine (docs/02-fan-out.md §13.3)

| Bureau score | Approve rate | APR range | Typical decline reasons |
|---|---|---|---|
| < 580 | 5% | 24–30% | low_score (95%) |
| 580–669 | 60% | 15–24% | low_score, high_dti |
| 670–739 | 90% | 10–15% | high_dti, income_unverified |
| 740+ | 98% | 6–10% | capacity_exceeded, manual_referral |

Hard rule: `requested_amount / annual_income > 0.5` ⇒ declined high_dti
regardless of score. Roughly 3% of approved decisions become 'referred'
(manual review queue).

## Runbook

### Alarm: lending-decisions-errors

**What it means.** ParentNotFound (apps or bureau missing for today),
or validation failure.

**First steps.**
1. Read the latest manifest sidecar.
2. If ParentNotFound: check `_SUCCESS` for both `loan_applications` and
   `credit_bureau_pulls` for today's ingest_date. Replay missing parent
   first, then this Lambda.
3. If validation failed: check schema_hash for drift.

### Alarm: lending-decisions-low-volume

**What it means.** Wrote < 10 000 rows (prod) / < 300 rows (test).

**First steps.**
1. Decisions is 1:1 with applications, but slightly less if bureau is
   missing rows. Check the join-misses log line.
2. Replay if config-driven; investigate join failures if not.

### Alarm: lending-decisions-freshness

**What it means.** No `heartbeat` for > 26h (prod) / > 12h (test).

**First steps.**
1. Confirm the schedule (`aws events describe-rule
   --name lending-decisions-daily`).
2. Walk upstream — apps and bureau both have to be fresh first.
