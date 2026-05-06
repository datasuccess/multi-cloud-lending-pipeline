# 02 — Phase 2: Fan-Out (six remaining batch generators)

Goal of this phase: bring the lending domain to life by adding the six
remaining batch sources on the **same Lambda + S3 pattern Phase 1 proved
out**, with cross-source referential integrity, per-source DLQs, and an
expanded alarm matrix. Phase 5 dbt then has a complete relational graph
to build a Data Vault on.

> Inherits Phase 1 wholesale. **What's new** is multi-Lambda
> orchestration without Airflow, FK consistency across sources, and a
> per-source schedule choreography. **What's still not here:** Iceberg
> (Phase 3), warehouses (Phase 4), dbt (Phase 5), Airflow (Phase 8),
> streaming (Phase 11).

---

## 1. Definition of done

A green Phase 2 PR means:

**Functional**
- [ ] Six new generators under `lambdas/<source>_generator/` each produce parquet + manifest + `_SUCCESS` per partition.
- [ ] One manual end-to-end smoke run produces a logically-consistent cohort:
      ~12k apps → ~12k bureau pulls → ~12k decisions (~75% approve) → ~9k drawdowns → ~30k payments → ~1-3k delinquency snapshot rows.
- [ ] Every downstream generator reads the latest `_SUCCESS`'d partition of its parent for FK values (no orphan FKs).
- [ ] Six new EventBridge rules cover the daily prod schedule (§5).
- [ ] Test-mode 6-hourly schedule extends consistently to all seven generators.

**Monitoring**
- [ ] 18 new CloudWatch alarms (3 per source × 6 sources), all wired to existing `lending-alerts-p1-page` SNS topic.
- [ ] CloudWatch dashboard widgets fill in the six placeholders Phase 1 reserved.
- [ ] Streamlit Pipeline Health page shows all seven sources without code changes.
- [ ] Runs ledger has rows from all seven sources; the ledger reader iterates over sources generically.

**Reliability**
- [ ] Each Lambda has an SQS DLQ wired via `DeadLetterConfig`. Async failures land there after max retries.
- [ ] DLQ depth alarm per Lambda: `ApproximateNumberOfMessagesVisible >= 1` for 5 min ⇒ P2 (email-only).
- [ ] Manual chaos run (force `ValidationFailed` in one downstream generator) confirms: errors alarm fires, parent partition unaffected.

**Security**
- [ ] IAM: each Lambda gets the *same* generator role (encrypt-only on the CMK, write to its own prefix, read parent prefix for FK lookup).
- [ ] PII tier annotations updated in `pii-handling.md`: `customers` is the highest-tier source.
- [ ] CloudTrail data events continue to capture S3 reads + writes across all seven prefixes.

**Cost**
- [ ] Total Phase 2 addition stays under $1/month (six more daily Lambda runs at the same right-sized shape).
- [ ] No new always-on infra. EventBridge + SQS + Lambda only.
- [ ] $5 budget alarm continues to be the structural cap.

**Docs**
- [ ] This file (`02-fan-out.md`) ships in the same PR.
- [ ] `validation.md` updated: per-source schemas now exist; Gate A's example table is generalized.
- [ ] `monitoring.md` updated: dashboard widget grid is complete.
- [ ] RUNBOOK.md gets a §"Phase 2" with the multi-source smoke + chaos commands.

---

## 2. The seven sources and their relationships

```
       customers ─────────────┐
        (slow-changing)        ├── loan_applications ── credit_bureau_pulls
                              │                     ├── loan_decisions
                              │                     │      │
                              │                     │      └── loan_drawdowns ── payments
                              │                     │                            │
                              │                     │                            └── delinquencies
                              │                     │                              (daily snapshot)
                              └─────────────────────┘
```

| Source | Grain | Parent(s) | Tier (PII) |
|---|---|---|---|
| `customers` | one row per applicant (95% new + 5% returning per day) | none | **High** — name, email, dob, address, phone |
| `loan_applications` (Phase 1) | one row per application | `customers` | Medium — amount, channel, applied_at; references customer |
| `credit_bureau_pulls` | one row per application | `loan_applications` | Low — bureau score, inquiry flag |
| `loan_decisions` | one row per application | `loan_applications` (+ bureau, logically) | Low — decision, APR, limit |
| `loan_drawdowns` | one row per *approved* loan | `loan_decisions` (decision='approved') | Medium — drawn amount, masked account_last4 |
| `payments` | one row per scheduled repayment event | `loan_drawdowns` | Low — amounts, status |
| `delinquencies` | one row per drawdown that's late, daily snapshot | `loan_drawdowns` + `payments` | Low — DPD bucket, outstanding |

---

## 3. Per-source schemas (pyarrow contract)

All schemas use the same primitives Phase 1 established: `decimal(12,2)`
for money, dictionary types for enums, `timestamp[us, UTC]` for time,
`date32` for dates. The schema-hash mechanism (Phase 1) extends to each
source independently.

### 3.1 `customers`
```
customer_id        string (uuid4)               PK, non-null
first_name         string                       PII tier 1
last_name          string                       PII tier 1
email              string                       PII tier 1
dob                date32                       PII tier 1
address_line1      string                       PII tier 1
city               string                       PII tier 2
state              string (dict, 50 US states)  PII tier 2
zip                string (5-digit)             PII tier 2
phone              string (E.164)               PII tier 1
kyc_status         dict {pending,verified,rejected,expired}
employment_status  dict {employed,self_employed,unemployed,retired,student}
annual_income      decimal(12,2)
created_at         timestamp[us, UTC]           non-null
updated_at         timestamp[us, UTC]           non-null (changes on returning customers)
is_returning       bool                         denormalized hint for dbt SCD2 in Phase 5
```

### 3.2 `credit_bureau_pulls`
```
pull_id            string (uuid4)               PK
application_id     string (FK → loan_applications.application_id)
customer_id        string (FK → customers.customer_id)
bureau_name        dict {experian,equifax,transunion}
bureau_score       int16                        300-850
hard_inquiry       bool
tradelines_count   int16                        0-30
delinquencies_count int16                       0-10
pulled_at          timestamp[us, UTC]
```

### 3.3 `loan_decisions`
```
decision_id        string (uuid4)               PK
application_id     string (FK)                  unique
customer_id        string (FK)
decision           dict {approved,declined,referred}
decision_reason    dict {clean,low_score,high_dti,income_unverified,
                          fraud_flag,capacity_exceeded,manual_referral}
apr_pct            decimal(5,2)                 nullable when declined
approved_amount    decimal(12,2)                nullable when declined
term_months        int16                        in {6,12,24,36,48,60}
decided_at         timestamp[us, UTC]
```

### 3.4 `loan_drawdowns`
```
drawdown_id        string (uuid4)               PK
decision_id        string (FK)                  decisions where decision='approved'
application_id     string (FK)
customer_id        string (FK)
drawn_amount       decimal(12,2)                ≤ approved_amount
account_last4      string (4 digits)            masked, last 4 only
disbursed_at       timestamp[us, UTC]
```

### 3.5 `payments`
```
payment_id         string (uuid4)               PK
drawdown_id        string (FK)
customer_id        string (FK)
scheduled_amount   decimal(12,2)
actual_amount      decimal(12,2)                0 when missed
principal_amount   decimal(12,2)
interest_amount    decimal(12,2)
payment_status     dict {scheduled,paid_full,paid_partial,missed,waived}
scheduled_at       date32
paid_at            timestamp[us, UTC]           nullable when status≠paid_*
```

### 3.6 `delinquencies`
```
snapshot_id        string (uuid4)               PK
drawdown_id        string (FK)
customer_id        string (FK)
dpd_days           int16                        days past due
dpd_bucket         dict {1-30,31-60,61-90,90+}
outstanding_principal decimal(12,2)
as_of_date         date32                       partition column
```

---

## 4. Cross-source referential integrity

Each downstream generator follows the same pattern:

```python
# parent_partition_uri() = "s3://bucket/raw/<parent>/ingest_date=YYYY-MM-DD/"
# Discovery: list-objects-v2 filtered by `_SUCCESS`, take latest ingest_date.
# Cheaper fast path: read the runs ledger (already on S3) for the parent's
# latest status=success row.

parent = read_parent_partition(source="loan_applications")
ids    = parent.column("application_id").to_pylist()
sample = random.sample(ids, k=N) if len(ids) >= N else random.choices(ids, k=N)
```

**Discovery rule.** Cheapest first: ledger fast-path (one `s3:GetObject`,
no LIST). Fallback: `s3:ListObjectsV2` filtered to `_SUCCESS` keys —
single API call covers all partitions. No new infra (no Glue catalog
yet — that's Phase 3).

**Sampling rule.**
- `loan_decisions`: 1 row per application → exhaustive iteration, not random sample.
- `credit_bureau_pulls`: same — 1 per application.
- `loan_drawdowns`: filtered to decisions where `decision='approved'`. ~75%.
- `payments`: each active drawdown contributes 1 scheduled payment per generator run (capped at 30 000/day total — see §7).
- `delinquencies`: snapshot of all drawdowns where the cumulative scheduled–actual gap is >0 by `as_of_date`.

**Returning customers.** `loan_applications` (Phase 1) currently invents
`customer_id` per row. Phase 2 inverts the dependency: generate
`customers` first, then `loan_applications` samples 95% net-new
+ 5% returning (sampled from prior `customers` partitions). One Phase 1
breaking change: `loan_applications` reads its parent before
generating, just like every other downstream. Behind a `MODE=test`
fallback that lets Phase 1 keep working standalone.

---

## 5. Schedule choreography

### Prod (daily)

| Time (UTC) | Source | Why this offset |
|---|---|---|
| 02:50 | `customers` | Parent for everything; runs first |
| 03:00 | `loan_applications` | Phase 1's existing slot |
| 03:10 | `credit_bureau_pulls` | Reads applications |
| 03:15 | `loan_decisions` | Reads applications + (optionally) bureau |
| 03:30 | `loan_drawdowns` | Reads decisions; only approved |
| 03:45 | `payments` | Reads drawdowns |
| 04:00 | `delinquencies` | Reads drawdowns + payments; end-of-day snapshot |

10–15 min gaps absorb Lambda cold-start variance and let parent partitions
finalize before children read. No DAG engine needed at this scale —
EventBridge is the orchestrator.

### Test mode (every 6h)

Same offsets, fired four times per day (00, 06, 12, 18 UTC). Anomaly
engine on for all seven generators, with mode-aware thresholds (Phase 1
pattern). Probabilities stay tuned to ~1 anomalous event per day across
the whole pipeline (not per source).

---

## 6. Failure modes and DLQs

Each Lambda gets `DeadLetterConfig.TargetArn` pointing to an SQS queue
named `lending-<source>-dlq`. EventBridge invocations are async; after
Lambda's two automatic retries fail, the failed event payload lands in
SQS. The queue has:

- **Retention:** 14 days (max). Long enough to investigate without
  building a separate replay system.
- **Visibility timeout:** 5 min (matches Lambda timeout × headroom).
- **Encryption:** SSE-SQS (not the project CMK — DLQs don't carry the
  loan data, just EventBridge invocation metadata).
- **Alarm:** `ApproximateNumberOfMessagesVisible >= 1` for 5 min ⇒ P2
  (email-only — DLQs are diagnostic, not page-worthy).

**What's deliberately NOT in Phase 2:**
- Auto-replay from DLQ. Phase 8 (Airflow) is the right place for that.
- Cross-source rollback. Each generator's `_SUCCESS` is per-partition;
  if `payments` fails on day N, days N-1 in upstream sources stay good.
  Phase 5 dbt's source-freshness check handles the consumer side.

---

## 7. Volume budget per source (per run)

Capped to keep every Lambda inside the **same 512 MB / 60s envelope**
Phase 1 right-sized. Realistic prod numbers in parens.

| Source | Cap | Realistic prod | Why capped |
|---|---|---|---|
| `customers` | 12 000 | 50–200 k/day | Match `loan_applications` volume; 95% are net-new |
| `loan_applications` | 12 000 (unchanged) | 50 k/day | Phase 1 |
| `credit_bureau_pulls` | 12 000 | 50 k/day | 1:1 with applications |
| `loan_decisions` | 12 000 | 50 k/day | 1:1 with applications |
| `loan_drawdowns` | ~9 000 | ~37 k/day | 75% approve rate |
| `payments` | **30 000 (cap)** | 60–100 k/day | Pure cost-control choice; pyarrow/RAM-limited at 512 MB |
| `delinquencies` | ~1 500 | ~5 k/day | Snapshot of late-portfolio subset |

The payments cap is the only deliberate scale-down. All others naturally
fit under 12k/run. The cap is documented in `cost-control.md` as the
"why payments is sub-realistic" cross-link.

---

## 8. Alarm matrix

Three alarms × six new sources = 18 new alarms. Same shapes as Phase 1
(`errors`, `freshness`, `low_volume`), thresholds parameterized per
source.

| Alarm | Watches | Prod threshold | Test threshold |
|---|---|---|---|
| `lending-<source>-errors` | `AWS/Lambda Errors` for that function | ≥ 1 in 5 min | same |
| `lending-<source>-freshness` | custom `heartbeat` metric | no data for 26 h | no data for 12 h |
| `lending-<source>-low-volume` | `RowsWritten` aggregated | < per-source floor / day | < per-source floor / run |

Per-source `MIN_ROWS` (prod): customers 8 000, applications 10 000 (Phase 1
keeps this), bureau 10 000, decisions 10 000, drawdowns 6 000, payments
20 000, delinquencies 1 000. Test mode floors are roughly 1/30 of prod
(matches Phase 1's 10 000 → 400 ratio).

Plus six DLQ-depth alarms (one per source). Total: **24 alarms in
Phase 2**, all wired to the same two SNS topics.

---

## 9. Cost

| Resource | Per-month addition |
|---|---|
| 6 × daily Lambda runs (arm64, 512 MB, ~10s avg) | ~$0.06 |
| 6 × SQS DLQ at near-zero traffic | ~$0.01 |
| 6 × EventBridge rules | free (covered by free tier) |
| 24 new CloudWatch alarms | $0.10/alarm/mo × 24 = $2.40 |
| EMF metric publishing | already included in Phase 1 cost |
| **Total Phase 2 addition** | **~$2.50/month** |

Still inside the $5 budget alarm Phase 1 set. Alarms are the dominant
new line item — at $0.10/alarm/month they add up faster than the Lambda
cost. Worth it: the alarm-per-(source × class) matrix is what makes the
fail-loud contract enforceable per-source.

---

## 10. Open implementation notes

- **Shared lib (`lambdas/shared/`) refactor.** Phase 1's storage / manifest /
  ledger / metrics modules are already source-agnostic — they take
  `source_name` as a parameter. Only addition: a `parent_partition.py`
  module exposing `latest_success(source: str) -> S3Path | None` with
  the ledger fast-path and the LIST fallback.
- **Schema drift across sources.** Each source has its own
  `schema.py` + baked-in hash. The hash check is unchanged; only the
  baseline differs.
- **Faker localization.** All US data (states, ZIPs, phone numbers).
  Locale fixed at `en_US` to keep PII shapes consistent for Phase 5
  dbt tests.
- **Idempotency.** Every generator uses the same partition convention
  (`ingest_date=YYYY-MM-DD`); re-running for the same date overwrites
  the parquet under that prefix. The ledger gets a *new* row per run
  (append-only — Phase 1 pattern).
- **Backfill.** `infra/03-invoke-and-backfill.sh` extends to a
  per-source argument: `backfill <source> <days>`. Default to running
  in dependency order if no source is specified.

---

## 11. What ships in this PR

```
lambdas/
├── shared/
│   └── parent_partition.py            # NEW: latest_success() + sample helpers
├── customer_generator/                # NEW
├── credit_bureau_pulls_generator/     # NEW
├── loan_decision_generator/           # NEW
├── loan_drawdown_generator/           # NEW
├── payment_generator/                 # NEW
└── delinquency_generator/             # NEW

infra/
├── lambda/
│   ├── deploy.sh                      # parameterized over source
│   ├── package-function.sh            # parameterized
│   └── 02-deploy-fanout.sh            # NEW: deploys all 6
├── 03-invoke-and-backfill.sh          # extended: per-source arg
├── 02-setup-monitoring.sh             # extended: 18 new alarms + 6 DLQ alarms
└── 06-set-mode.sh                     # extended: schedules all 7

docs/
├── 02-fan-out.md                      # this file
├── validation.md                      # touched: per-source Gate A examples
├── monitoring.md                      # touched: dashboard grid completed
└── pii-handling.md                    # touched: per-source tier annotations

tests/
├── test_customers_generator.py        # NEW
├── test_bureau_pulls_generator.py     # NEW
├── test_loan_decisions_generator.py   # NEW
├── test_loan_drawdowns_generator.py   # NEW
├── test_payments_generator.py         # NEW
├── test_delinquencies_generator.py    # NEW
└── test_fan_out_e2e.py                # NEW: cross-source integrity
```

---

## 12. Out of scope for Phase 2

- Iceberg / Glue catalog (Phase 3)
- Snowflake or Redshift loaders (Phase 4)
- dbt models (Phase 5)
- Airflow orchestration / DLQ replay (Phase 8)
- Streaming `loan_decisions` variant (Phase 11)
- Cross-account / multi-region (Phase 8 if at all)

---

## 13. Real-world realism rules

These rules make the synthetic data look like prod. dbt source tests in
Phase 5 expect them — break a rule here and a Phase 5 test fails.

### 13.1 Temporal consistency (the big one)

- `customers.created_at` is **always before** any
  `loan_applications.applied_at` referencing that customer.
- Returning customers preserve their original `created_at`; only
  `updated_at` advances.
- `bureau_pulls.pulled_at` precedes `decisions.decided_at` by 1–10 minutes.
- `decisions.decided_at` precedes `drawdowns.disbursed_at` by 0–48 hours.
- `payments.scheduled_at` is monotonic per drawdown; `payments.paid_at`
  (if not null) is within ±5 days of `scheduled_at`.
- `delinquencies.as_of_date` is the partition column; rows are derived
  from drawdowns + payments at end-of-day.

### 13.2 Customer lifecycle

- **New customer KYC:** 70% `pending`, 28% `verified`, 1% `rejected`,
  1% `expired`. `pending` ones eventually transition to `verified` (or
  `rejected`) by the time their first application is decided.
- **Returning customer KYC:** 96% `verified`, 3% `expired`, 1%
  `rejected` (these last two re-enter the KYC flow).
- **Address stability:** 90% of returning customers keep their address.
  10% have moved (50% same state, 50% different state).
- **Income drift:** returning customers' `annual_income` jitters by a
  log-normal multiplier with mean 1.0 and σ ≈ 5%, clamped to ±25%.
- **Employment stability:** 92% of returning customers keep their
  `employment_status`; 8% transition (employed ↔ self_employed most
  common, employed → unemployed for ~1%).

### 13.3 Decision rules (not pure random)

A small rule engine in `loan_decision_generator`. Inputs: bureau score
band, requested amount, declared income.

| Bureau score | Approve rate | Typical reasons (declined) |
|---|---|---|
| < 580 | 5% | `low_score` (95%) |
| 580–669 | 60% | `low_score`, `high_dti` |
| 670–739 | 90% | `high_dti`, `income_unverified` |
| 740+ | 98% | `capacity_exceeded` (rare), `manual_referral` |

Plus a hard rule: `requested_amount / annual_income > 0.5` ⇒ declined
with `high_dti`, regardless of score.

**APR conditional on score band** (real lending tier shapes):
- 740+ → 6–10%
- 670–739 → 10–15%
- 580–669 → 15–24%
- declined / referred → null

### 13.4 Drawdown patterns

- Only decisions where `decision='approved'` produce drawdowns.
- 70% of approved customers draw the **full** approved amount.
- 30% draw partial: uniform between 30% and 99% of approved amount.
- `account_last4` is masked (last 4 of a 16-digit number).
- Time-to-draw: 0–48 hours after `decided_at`, log-normal skewed early.

### 13.5 Payment realism (Markov-ish)

For each active drawdown, generate one scheduled payment per period
(simplified to one per generator run). Payment status follows a
state-dependent distribution:

| Prior payment status | This payment: paid_full | paid_partial | missed |
|---|---|---|---|
| paid_full / new | 92% | 5% | 3% |
| paid_partial | 60% | 25% | 15% |
| missed | 35% | 25% | 40% |

Once you miss, you're more likely to miss again — the cascade is what
makes Phase 5 dbt's delinquency staging non-trivial.

- `principal_amount` + `interest_amount` ≈ `actual_amount`. Interest is
  ~APR / 12 of remaining principal; principal is the rest.
- `paid_at` is null when `payment_status ∈ {scheduled, missed, waived}`.

### 13.6 Delinquencies are derived, not invented

`delinquency_generator` reads `drawdowns` + `payments`, computes the
cumulative scheduled-vs-actual gap per drawdown as of the run date, and
emits one snapshot row per drawdown where `dpd_days > 0`. The DPD
bucket is derived:

```
1–30   → "1-30"
31–60  → "31-60"
61–90  → "61-90"
> 90   → "90+"
```

No randomness in this generator — it's a deterministic function of
upstream data. The only randomness is the choice of
`as_of_date = ingest_date`.

### 13.7 Where these rules are tested

- Per-generator unit tests assert distributions match within tolerance
  (e.g., approve rate by score band, payment-status transition matrix).
- `tests/test_fan_out_e2e.py` runs a small cohort end-to-end and asserts
  every FK resolves, no temporal violations, derived delinquency rows
  match independently-computed expectations.
- Phase 5 dbt source tests are the production-style enforcement layer
  (uniqueness, non-null, referential integrity, accepted-values).
