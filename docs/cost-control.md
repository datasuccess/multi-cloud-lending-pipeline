# Cost control — strategy across all phases

Cross-phase doc. Every phase that adds a billable resource points back here.

> **Operating principle.** Cost is bounded by **structure**, not by
> discipline. Every infra script that adds a billable resource adds it with
> a quota, a lifecycle, and a comment justifying the cost. If we ever need
> to remember to "watch costs," the design has already failed.

The mechanisms below are layered. Each one independently caps a different
class of cost; together they make a runaway bill structurally impossible.

## 1. Hard budget alarm

`infra/00-setup-foundations.sh` provisions an **AWS Budget** at $5/month
for the project, with SNS notifications at 80% and 100%. If anything in
this project starts costing real money, it pages immediately.

For learning projects, $5 is the "this should be ~free, anything more is a
bug" floor. Production phases (Phase 4+) bump the budget per phase; the
alarm itself is the same mechanism.

## 2. Reserved concurrency on every Lambda

The Phase 1 generator is configured for `reserved-concurrent-executions=2`.
Even if a misconfigured event source fires the function 10 000 times in a
minute, only 2 run concurrently. A runaway loop costs cents, not hundreds
of dollars.

Account quota on a fresh AWS account refuses to drop unreserved
concurrency below 10, so `deploy.sh` makes the call non-fatal — but the
intent is in the script, and on any account that allows it the cap is
hard. Phase 2+ Lambdas inherit the same pattern.

## 3. Lambda right-sized — not generously sized

The generator runs at 512 MB / 60s timeout. Measured behavior:

| Memory | Typical run | Per-invocation cost (arm64, us-east-1) |
|---|---|---|
| 256 MB | OOM-risk; ~120s on 12k rows (CPU-starved) | $0.0005 |
| **512 MB** | **~7s nominal, ~37s under chaos slow** | **$0.00006** |
| 1024 MB | ~5s nominal | $0.00009 |
| 2048 MB | ~4s nominal | $0.00014 |

512 MB is the sweet spot. Going higher costs more per ms with marginal
speedup; going lower risks OOM and slows down enough that the per-ms
savings disappear. The principle: **right-size to actual measurements,
not to defaults**.

## 4. arm64 (Graviton) over x86_64

~20% cheaper per ms for the same Lambda code. The build pipeline pins
`--platform manylinux2014_aarch64` so the layer wheels match the runtime.
The architecture is fixed at create-time, so this is a one-decision
saving — every invocation forever benefits.

## 5. Test mode keeps test runs cheap

`infra/06-set-mode.sh test` sets:

- Schedule: hourly (vs daily in prod)
- `ROWS_PER_RUN=2000` (vs 12 000 in prod)
- Anomaly engine: on (with non-zero probabilities)

That's 24 runs/day × 2k rows ≈ ~$0.05/month even running constantly. Prod
mode (1 run/day × 12k rows) drops to ~$0.02/month. The whole Lambda
budget is essentially zero — which is the point. We can leave test mode on
for weeks while learning, with no cost discipline required.

## 6. S3 lifecycle on noncurrent versions

Bucket has versioning **on** (recovery from accidental deletes) but
**noncurrent versions expire after 30 days**.

Without lifecycle: a year of daily backfills with corrections would
multiply storage cost by ~365×. With it: 30-day recovery window, then the
cost amortizes back to "current data only."

Phase 6+ adds a Glacier Deep Archive tier for `raw/` after 1 year (~95%
cheaper than Standard). Phase 1's data is too small (~50 MB total) to
bother — the rule is in the script, the cost saving is academic at this
scale.

## 7. No NAT, no VPC, no private endpoints in Phase 1

NAT gateways alone are $32/month minimum — more than the entire Phase 1
budget. Phase 1 deliberately doesn't need a VPC: the Lambda runs in the
AWS-managed pool, calls public S3 + KMS endpoints, IAM-gated.

Phase 4 introduces VPC endpoints with a cost/benefit:
- **Gateway endpoints for S3**: free.
- **Interface endpoints for KMS**: $7/month each.

We add them only when Phase 4's loaders need to run inside a VPC for
network isolation. Until then, public endpoints + IAM is the right
trade-off.

## 8. CloudWatch Logs retention capped

`deploy.sh` sets `--retention-in-days 7` on
`/aws/lambda/lending-loan-app-generator`. Default is "never expire" —
which on a chatty Lambda is a slow cost leak.

7 days is more than enough for incident debugging. Longer-term retention
lives in two cheaper places:

- **The runs ledger** (S3 JSONL) — pennies per year, queryable forever.
- **CloudTrail** (separate audit trail) — required for compliance, lives
  in the audit bucket with its own 7-year retention.

The principle: each storage system holds the data it's *cheapest* for.
CloudWatch Logs is for hot debugging; long-term observability lives in S3.

## 9. The architecture itself is cost-conscious

Composing the choices above:

- **Parquet + Snappy** = ~3 MB per 12k rows ≈ $0.07/GB/month → cents per
  year for the entire Phase 1 dataset.
- **One Lambda layer for the heavy deps** = uploaded once, version-pinned,
  attached to as many Lambdas as Phase 2+ needs without re-uploading
  ~50 MB of pyarrow each time.
- **EventBridge for scheduling** instead of Step Functions — free at
  this volume; Step Functions starts at $0.025/1000 transitions.
- **No always-on infra in Phase 1** — every billable resource is either
  per-invocation (Lambda, KMS), per-byte (S3), or already-included
  (CloudWatch metrics under the free tier).

## 10. Per-phase projections

The CLAUDE.md tracks an "expected monthly cost" per phase:

| Phase | Adds | Projected cost |
|---|---|---|
| 1 | Lambda + S3 + KMS + CloudWatch + CloudTrail + Secrets Manager | ~$0.50/month |
| 2 | Streaming consumer (Kinesis or MSK Serverless) | ~$5/month |
| 4 | Snowflake credits + small Redshift Serverless on demand | ~$15/month |
| 5 | dbt Cloud free tier (or self-hosted dbt Core) + Airflow MWAA *only* during dev windows | ~$10/month |
| 6 | Iceberg (S3 + Glue catalog) + Athena queries | query-priced; ~$5/month at low volume |
| 8 | Hosted Streamlit + multi-account split | ~$25/month |

Each phase's `infra/<phase>/setup.sh` carries a cost note saying *what
gets added* and *roughly how much*. If the running sum exceeds the
configured budget, the alarm fires.

## 11. Anti-patterns we avoid

- ❌ **Cost dashboards as the primary control.** Dashboards are for
  *retrospective* debugging. The active controls are budgets + quotas +
  lifecycles. If the only thing standing between you and a $1000 bill is
  someone remembering to check Cost Explorer, you've already lost.
- ❌ **Always-on infrastructure for development.** Dev Redshift clusters
  that run 24/7 dominate every other line item. Phase 4+ uses Redshift
  Serverless (per-second) and Snowflake compute (per-second), both auto-suspend.
- ❌ **Logging at default retention.** "Never expire" on CloudWatch Logs
  is the most common silent cost leak in AWS. Cap it at write time, not
  at the end of the month.
- ❌ **Forgotten test resources.** `infra/99-teardown.sh` exists so any
  resource Phase 1 created can be deleted in one command. If you can't
  tear down, you can't experiment cheaply.

## 12. Where the cost controls live in code

| Control | File |
|---|---|
| $5 budget alarm | `infra/00-setup-foundations.sh` |
| Reserved concurrency | `infra/lambda/deploy.sh` |
| Lambda memory + timeout | `infra/lambda/deploy.sh` |
| arm64 architecture | `infra/lambda/deploy.sh` (immutable post-create) |
| S3 versioning + lifecycle | `infra/00-setup-foundations.sh` |
| CloudWatch Logs retention | `infra/lambda/deploy.sh` |
| Test/prod mode toggle | `infra/06-set-mode.sh` |
| Per-phase cost projection | each phase's plan doc + `CLAUDE.md` |
| Teardown | `infra/99-teardown.sh` |
