# Monitoring & observability — strategy across phases

Cross-phase doc. Every phase that adds a new pipeline component points back here.

> **Operating principle.** Monitoring exists to answer *5 questions, every day, without anyone logging in*. If a question can only be answered by SSHing into a box or running a one-off query, it isn't monitored.

---

## 1. The five questions

Every batch job, streaming consumer, dbt run, and warehouse load must answer:

1. **Did it run?** — schedule fired, process started.
2. **Did it succeed?** — finished without exception.
3. **Did it produce the right shape?** — row count, schema, key columns within bounds.
4. **Is it fresh enough?** — last successful output is more recent than the downstream SLA.
5. **Is anything trending wrong?** — volume drift, cost spike, unusual access.

If a component cannot answer all 5 from observability alone, it isn't done.

## 2. The five layers

| Layer        | Watches                                       | Wakes someone up?      |
|--------------|-----------------------------------------------|------------------------|
| Operational  | Process invocations, errors, durations         | Yes (P1)               |
| Freshness    | Last-success age vs SLA                        | Yes (P1 if breached)   |
| Quality      | Schema, counts, nulls, uniqueness              | Yes (P2)               |
| Business     | Volume trends, KPI drift                       | No (dashboard only)    |
| Security     | Auth events, PII access, KMS anomalies          | Yes (P1, security on-call) |

P1 = page; P2 = email; P3 = ticket.

## 3. Tooling matrix — what we use in which phase

| Layer       | Phase 1                                     | Phase 2-3                              | Phase 4-6 (warehouse)                   | Phase 8-9 (orchestration + dashboard)        |
|-------------|---------------------------------------------|----------------------------------------|------------------------------------------|----------------------------------------------|
| Operational | CW Metrics (Powertools EMF), CW Alarms, SNS | + Lambda Insights extension, DLQ      | + dbt run results published to CW        | + Airflow SLA misses, Slack via SNS          |
| Freshness   | `last_success_age_hours` custom metric      | + per-source freshness alarms         | + `dbt source freshness` (Phase 5)       | + Streamlit "Pipeline Health" page (Phase 9) |
| Quality     | Manifest sidecar + post-write validation    | + DLQ on validation fail              | + dbt tests (schema, not_null, unique, accepted_values, custom) | + Elementary or Soda layered on dbt          |
| Business    | EMF dimensional metrics (by channel, etc.)  | + per-source metrics                  | + warehouse-side KPIs from marts         | + Streamlit business-KPI pages               |
| Security    | CloudTrail data events on KMS+S3 (Phase 1)  | —                                      | + Snowflake/RS query-history snapshots   | + audit dashboard, off-hours alarms          |

Tools we **don't** use (and why):

- **Monte Carlo / Bigeye / Datafold (SaaS data observability)** — overkill for solo learning project. Same patterns reproducible with dbt + Elementary + CloudWatch.
- **Datadog / New Relic** — paid; CloudWatch + Grafana cover the operational layer for free.
- **Great Expectations** — heavyweight; dbt tests + custom dbt-test macros do 90% of what GX offers without the second toolchain.

## 4. SLOs (the targets — written down and tracked)

| SLO                                                 | Target          | Phase 1 verifies via             |
|-----------------------------------------------------|-----------------|----------------------------------|
| Freshness: `loan_applications` lands by 03:15 UTC   | 99% of days     | `last_success_age_hours` alarm   |
| Completeness: ≥10,000 rows per daily file           | 100% of runs    | `rows_written` alarm             |
| Schema: every file matches `parquet_writer.SCHEMA`  | 100% of runs    | post-write validation            |
| Availability: Lambda invocation success             | 99% over 30d    | `Errors` alarm + monthly review  |
| Cost: project total ≤ $50/month                     | 100% of months  | AWS Budget alarm                 |
| PII access: zero unauthorised AssumeRole            | 100%            | CloudTrail alarm (Phase 4)       |

SLOs are **published to consumers**, not internal. When dbt staging in Phase 4 reads from S3, its source freshness check uses the same 03:15 UTC target.

## 5. The three patterns every prod data job ships with

### 5.1 `_SUCCESS` marker + manifest sidecar

Hadoop/Spark made this standard for a reason: **file presence is not a success signal**.

```
s3://lending-raw-<acct>/raw/loan_applications/ingest_date=2026-05-04/
├── 2026-05-04T03-00-00Z_<uuid>.parquet
├── 2026-05-04T03-00-00Z_<uuid>.parquet.manifest.json
└── _SUCCESS                                              ← written last, atomic
```

Manifest schema:

```json
{
  "run_id": "5d9c…",
  "source": "loan_applications",
  "generator_version": "loan_app/0.1.0",
  "ingest_date": "2026-05-04",
  "parquet_key": "raw/loan_applications/ingest_date=2026-05-04/2026-05-04T03-00-00Z_5d9c.parquet",
  "rows": 12000,
  "bytes": 3145728,
  "schema_hash": "sha256:8f3a…",
  "started_at": "2026-05-04T03:00:00.123Z",
  "finished_at": "2026-05-04T03:00:04.876Z",
  "duration_ms": 4753,
  "validation_passed": true
}
```

**Rules:**
- Write parquet → write manifest → write `_SUCCESS`. Strictly in that order.
- Downstream readers (Snowflake `COPY`, dbt source, Spark batch) wait for `_SUCCESS`.
- If `_SUCCESS` is missing, the partition is "in progress or failed" — never "done with what's there".
- Replays delete the whole partition (`_SUCCESS` first, then files) before re-writing.

### 5.2 Pipeline runs ledger

A single S3 path that *every* job appends to. The single source of truth for "did X run on day Y."

```
s3://lending-raw-<acct>/_pipeline_runs/
└── source=loan_applications/
    └── year=2026/month=05/day=04/
        └── run-5d9c.jsonl
```

JSONL format — one record per attempt (success and failure):

```json
{"run_id": "5d9c…", "source": "loan_applications", "started_at": "...", "finished_at": "...",
 "status": "success", "rows": 12000, "bytes": 3145728, "duration_ms": 4753,
 "trigger": "eventbridge:lending-loan-app-daily", "lambda_request_id": "..."}
```

Why JSONL not Parquet? Append-friendly without rewriting. We compact monthly (Phase 3 Iceberg job).

Why a separate ledger when CloudWatch logs exist? **Retention.** CW logs are 7 days here. The ledger lives forever. When someone asks "did the pipeline run on March 14?", the answer is one S3 ls + one jq away.

### 5.3 Post-write validation (the silent-failure killer)

A job that writes 0 rows and says "success" is the canonical 3am page. Defend with a read-after-write check:

```python
# pseudocode in handler.py
parquet_key = write_parquet(rows)
table = pq.read_table(f"s3://{bucket}/{parquet_key}")

assert table.num_rows == len(rows), "row count mismatch"
assert table.schema.equals(EXPECTED_SCHEMA, check_metadata=False), "schema drift"
assert table.num_rows >= MIN_ROWS, f"too few rows: {table.num_rows} < {MIN_ROWS}"

write_manifest(... validation_passed=True)
write_success_marker()
```

If any assertion fails: skip `_SUCCESS`, write the manifest with `validation_passed=false` and `validation_errors=[…]`, raise — Lambda fails, CW alarm fires.

**Cost:** one extra S3 GetObject (~1 ms, fractions of a cent). Worth it.

## 6. Alerting model (the SNS hierarchy)

Three SNS topics, each with its own subscriber list:

| Topic                      | Severity | Latency target | Subscribers (this project)               |
|----------------------------|----------|----------------|------------------------------------------|
| `lending-alerts-p1-page`   | P1       | 15 min         | email (you), Slack `#lending-incidents`  |
| `lending-alerts-p2-email`  | P2       | 1 business day | email (you)                              |
| `lending-alerts-p3-ticket` | P3       | next sprint    | email (you), eventually GitHub issue     |

In real teams P1 routes to PagerDuty/Opsgenie. We mock with email; the **separation of severity** is what matters, so signal stays signal.

**Routing examples:**

| Condition                                            | Topic |
|------------------------------------------------------|-------|
| Lambda Errors ≥ 1                                    | P1    |
| `last_success_age_hours` > 25                        | P1    |
| `rows_written` < 10,000                              | P2    |
| Schema drift (validation fail)                       | P1    |
| Cost > $50/mo (Budget)                               | P2    |
| Cost > $100/mo                                       | P1    |
| Unauthorised PII AssumeRole attempt                  | P1    |
| Drift in `applications_by_channel` distribution > 3σ | P3    |

## 7. Dashboards

- **CloudWatch dashboard `lending-pipeline`** — operational view, one widget per source. Phase 1 ships the loan_applications widget; Phase 2 fills the rest.
- **Streamlit "Pipeline Health" page** — Phase 9. Reads the runs ledger and warehouse `INFORMATION_SCHEMA` to show: per-source freshness, row counts last 30d, validation success rate, cost trend, last 10 alerts.

We do **not** build a Grafana stack. The CW dashboard plus Streamlit covers the use cases at zero extra infra.

## 8. Heartbeats and synthetic checks

Two cheap patterns that catch failures the metrics miss:

1. **Heartbeat metric.** Every successful run emits `Heartbeats=1`. The freshness alarm watches "no heartbeat in 25h" — fires even if Lambda was *never invoked* (EventBridge misconfigured, account suspended, etc.).
2. **Synthetic canary.** Phase 8 adds a tiny daily Lambda that asserts "yesterday's `_SUCCESS` exists and manifest passes basic shape checks." Catches the rare case where the alert pipeline itself is broken.

## 9. Anti-patterns (what we deliberately don't do)

- ❌ **Email on every successful run.** Trains people to ignore alerts. Notify on *failure*, summarise success in a daily digest.
- ❌ **Alarm on every metric.** Pick the SLO-violating ones. Everything else is dashboard material.
- ❌ **Logs as metrics.** Don't `grep | wc -l` log lines for monitoring. Emit a metric.
- ❌ **No-runbook alarms.** Every alarm has a runbook line in `lambdas/<source>/README.md` saying "if this fires, do X." If you can't write the runbook, the alarm shouldn't exist.
- ❌ **Threshold-only alarms on long-trending metrics.** Use anomaly detection (Phase 8+) for things like volumes that drift seasonally.
- ❌ **Monitoring built after the fact.** Ship monitoring with the feature in the same PR. Phase 1 includes monitoring; Phase 1 doesn't ship without it.

## 10. Per-phase additions (cumulative)

| Phase | Adds                                                                                          |
|-------|-----------------------------------------------------------------------------------------------|
| 1     | EMF metrics, manifest sidecar, runs ledger, post-write validation, 3 CW alarms, SNS topics, runbook |
| 2     | Per-source widgets, Lambda Insights, DLQ + redrive alarms, full dashboard                      |
| 3     | Iceberg snapshot count metric, compaction job duration                                         |
| 4     | dbt test results published to CW, source freshness checks                                      |
| 5     | DV2 model row-count assertions, hash-key collision check                                       |
| 6     | Snowflake QUERY_HISTORY daily snapshot to S3 + ledger                                          |
| 7     | dbt cross-warehouse test parity (Snowflake & Redshift counts agree within 0.1%)                |
| 8     | MWAA / Airflow SLA misses, synthetic canary, anomaly-detection alarms                          |
| 9     | Streamlit "Pipeline Health" page                                                               |
| 10    | BigQuery INFORMATION_SCHEMA snapshot, BigLake freshness                                        |
| 11    | Streaming lag (consumer offset vs producer head), DLT (dead-letter topic) alarms               |
| 12    | (stretch) Open-Lineage events to Marquez                                                       |
| 13    | Cost-per-source breakdown, per-warehouse perf comparison                                        |

## 11. The runbook discipline

Every alarm in this project has, in `lambdas/<source>/README.md` or `docs/runbooks/<area>.md`:

```markdown
### Alarm: lending-loan-app-low-volume

**What it means.** Yesterday's run wrote fewer than 10,000 rows.

**Likely causes.**
1. Lambda env var `ROWS_PER_RUN` was changed.
2. Generator failed mid-run after writing partial output (manifest will show `validation_passed=false`).
3. Upstream Faker/seed change produced unexpected dropouts.

**First steps.**
1. `aws s3 ls s3://lending-raw-<acct>/_pipeline_runs/source=loan_applications/year=YYYY/month=MM/day=DD/`
2. Inspect the latest `run.jsonl` — `status`, `rows`, `validation_errors`.
3. Check CloudWatch logs for the run_id.
4. If the run is irrecoverable, replay: `aws lambda invoke --payload '{"ingest_date":"YYYY-MM-DD"}' …`
```

If a runbook entry doesn't exist, the alarm doesn't ship. This is enforced in PR review.

## 12. What "done" looks like for monitoring in Phase 1

Phase 1 PR is not mergeable until:

- [ ] Powertools `Logger` + `Metrics` wired in handler
- [ ] At least 4 EMF metrics published (`rows_written`, `bytes_written`, `duration_ms`, `heartbeat`)
- [ ] Manifest sidecar + `_SUCCESS` marker written in correct order
- [ ] Pipeline runs ledger appends one line per invocation
- [ ] Post-write validation reads parquet back and asserts shape
- [ ] 3 CW alarms exist and route to SNS topics
- [ ] SNS email subscription confirmed
- [ ] Runbook entries exist for all 3 alarms
- [ ] Manual test: introduce a deliberate validation failure → confirm alarm fires
