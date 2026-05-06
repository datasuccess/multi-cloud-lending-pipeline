# Validation taxonomy

The pipeline's validation strategy is a **four-gate** model. Each gate catches
a class of failure the previous gate cannot see. The contract any downstream
consumer relies on is **not** "the data is correct" — it is:

> If `_SUCCESS` exists for partition X, all four gates were green for that
> partition.

That is a narrow, verifiable promise. Everything in the pipeline either
upholds the promise or refuses to write `_SUCCESS`.

## Escalation policy: what we do when a gate fails

The four gates below are the **what** — *what* we check. Once a check fails,
we still need a policy: *what to do about it*. There are three industry
options; we pick one and the choice has architectural consequences.

| Policy | Behavior on gate failure | When it's the right pick | Why we don't (or do) use it |
|---|---|---|---|
| **Fail loud** | Whole partition is rejected. No `_SUCCESS`. Alarm fires. Ledger row marked `status=failure`. Bad parquet may exist as an orphan but no consumer will read it. | Batch jobs where the partition is the unit of consumption. Replays are cheap. | **Our default.** The lending pipeline produces `loan_applications` partitions consumed atomically by Phase 4 loaders and Phase 5 dbt models. Splitting a partition makes no business sense. |
| **Silent corruption** | The bad data flows through unchecked. Eventually a consumer notices (or doesn't). | Never. This is the **anti-pattern** every other policy is designed to prevent. | We refuse it. The four gates exist exactly so this cannot happen — `_SUCCESS` is the only "go" signal and is gated on every check. |
| **Quarantine / dead-letter** | Bad rows are routed to a side prefix (`_quarantine/<source>/`), the rest of the partition flows. Quarantined rows are alerted on but don't block the path. | Streaming pipelines where a single bad event shouldn't stop the consumer. Per-row sources where there's no natural partition unit. | **Not in Phase 1.** The generator is the system of record — there's no upstream to quarantine *from*. Phase 2's streaming path will use this for the per-event `loan_decision` stream, where one bad event must not stall the consumer for everyone else. |

The choice is per-source, not per-pipeline: a single project can mix
fail-loud (batch parquet) with quarantine (streaming events) — Phase 2
will. Phase 1 is fail-loud only.

### What "fail loud" actually buys you

Three guarantees, none of them trivial:

1. **Recoverability.** Replay is one command. The ledger has the run, the
   manifest has the schema hash, the Lambda is idempotent — re-running
   produces a complete, valid partition.
2. **Bounded blast radius.** A bad day on the producer cannot propagate
   downstream. Snowflake/Redshift/dbt will sit idle on the broken date
   rather than load half-data.
3. **Visible accountability.** The alarm + ledger row is the audit trail.
   "Why was 2026-05-06 missing in the warehouse?" has a one-line answer.

The cost: **partition-granular outages**. If gate A rejects one bad row, the
whole 12 000-row partition is rejected (we don't quarantine the row, we
reject the run). For batch loan applications that's correct — partial data
on a financial date would be worse than no data. For per-event streams it
would be wrong, hence Phase 2 picks differently.

## The four gates

### Gate A — Generation-time (in-memory, before any S3 call)

| Check | Mechanism | Example failure |
|---|---|---|
| Type & nullability | pyarrow `Schema` construction | `null` in `applied_at` (non-null `timestamp` column) ⇒ `ArrowInvalid` |
| Enum domain | dictionary types in the schema | `channel="fax"` (not in {web, mobile, broker, branch}) |
| Decimal precision | `decimal(12,2)` | `1234567890123.45` exceeds 10 digits before the decimal |
| Date/timestamp shape | `date32` / `timestamp[us, UTC]` | string `"yesterday"` instead of an ISO timestamp |

Failures at this gate raise inside `pa.Table.from_pylist(...)` and the run
aborts **before any S3 write**.

### Gate B — Post-write content validation

After parquet hits S3, before `_SUCCESS` is written.

| Check | Mechanism | Default (prod) | Default (test) |
|---|---|---|---|
| Volume floor | `len(rows) >= MIN_ROWS` | 10 000 | 1 |
| Schema-hash stability | `sha256(schema.serialize())` matches the baseline baked into generator code | strict | strict |

Failure raises `ValidationFailed`. The orphan parquet remains on S3 (small,
KMS-encrypted, lifecycle-cleaned eventually) but **no `_SUCCESS` is written**.
The chaos test (`infra/04-chaos-test.sh`) deliberately trips this gate with
`rows=5`.

### Gate C — Operational (CloudWatch, after the run completes)

These catch the failure modes A and B cannot see — primarily: **the lambda
didn't run at all**, or it ran many times with subtly low volumes.

| Alarm | Watches | Default (prod) | Default (test) |
|---|---|---|---|
| `lending-loan-app-errors` | AWS/Lambda `Errors` (any uncaught exception) | ≥ 1 in 5 min | same |
| `lending-loan-app-freshness` | custom `RowsWritten` metric | no data for 26 h | 90 min |
| `lending-loan-app-low-volume` | `RowsWritten` aggregated | < 10 000 / day | < 400 / run |

Operational gates catch:
- Schedule failure (EventBridge stopped, IAM revoked, account quota tripped).
- Drift: every individual run validates but the daily aggregate is suspiciously
  low — the "five UNDERSHOOT runs in a row" failure that A/B cannot see.
- Infra failures inside the lambda (S3 throttle, KMS unavailable, layer
  resolution failed) — these fail before A/B even run, so only the `Errors`
  alarm catches them.

### Gate D — Consumer-side (Phase 4+)

Every downstream consumer enforces its own preconditions:

| Check | Where | When |
|---|---|---|
| `_SUCCESS` precondition | Snowflake / Redshift COPY job | refuses to load a partition without `_SUCCESS` |
| `schema_hash` matches expected | Loader compares manifest's `schema_hash` to the value pinned in code | fails the load on mismatch (catches "schema drifted but A/B passed because the new schema is also valid") |
| dbt source freshness | dbt `sources.yml` | `applied_at` max within 24 h |
| dbt source tests | dbt | unique `application_id`, non-null PII fields, referential integrity |

Consumer-side gates exist because **upstream validation cannot anticipate
every downstream concern**. A schema change might be backwards-compatible for
the producer (gate A passes) but break a dbt model (gate D fails).

## Why four gates and not, say, two

Each gate covers a class of failure no other gate can:

| Failure mode | A | B | C | D |
|---|---|---|---|---|
| Type or enum violation in row data | ✔ | | | |
| Row count too low | | ✔ | | (✔ via partial loads) |
| Schema drift between code and write | | ✔ | | ✔ |
| Lambda crashes mid-run | | | ✔ | |
| Lambda doesn't run at all | | | ✔ | |
| KMS / S3 service blip | | | ✔ | |
| Slow drift across many runs | | | ✔ | |
| Schema valid but breaks a dbt model | | | | ✔ |
| Partition read before `_SUCCESS` | | | | ✔ |

A two-gate model (A + C, or B + D) would always have a failure mode it
cannot detect. Four is the minimum that closes the matrix.

## Where the gates live in code

- **Gate A**: `lambdas/loan_application_generator/handler.py:run` (the
  `pa.Table.from_pylist(...)` line) and the schema definition in
  `lambdas/loan_application_generator/schema.py`.
- **Gate B**: same file, the post-write `validate_run` block; `MIN_ROWS` is
  read from the env (`infra/06-set-mode.sh` flips it).
- **Gate C**: `infra/02-setup-monitoring.sh` provisions the alarms;
  `06-set-mode.sh` re-tunes thresholds when mode flips.
- **Gate D**: ships in Phase 4 (loaders) and Phase 5 (dbt sources).

## What an end-to-end fail-loud incident looks like

A bad day, walked through:

1. **02:55 UTC** — EventBridge fires `lending-loan-app-daily`.
2. **02:55:12** — handler runs gate A; pyarrow rejects a row because some
   upstream change introduced a `null` in `applied_at`. `ArrowInvalid` raises.
3. **02:55:13** — `lambda_handler`'s outermost `except` writes a ledger entry
   with `status=failure`, `error="ArrowInvalid: null in non-null column…"`.
4. **02:55:14** — lambda exits with an unhandled exception (re-raised after
   the ledger write). `AWS/Lambda Errors` increments by 1.
5. **02:55:14** — `_SUCCESS` does **not** exist for `ingest_date=2026-05-06`.
   No parquet is written either, because gate A is pre-write.
6. **03:00:14** — `lending-loan-app-errors` evaluates: `Errors >= 1`, alarm
   transitions OK → ALARM. SNS publishes.
7. **03:00:14** — Streamlit's "Pipeline Health" page (60 s ledger TTL) shows
   the failure within ~1 min; CloudWatch dashboard shows the error spike.
8. **Phase 4 loader** wakes up at its scheduled time, looks for
   `_SUCCESS` under `ingest_date=2026-05-06/`, doesn't find it, **logs and
   skips**. No corrupt data lands in Snowflake.

That sequence is the whole "fail loud" contract working: visible, recoverable,
no downstream contamination.
