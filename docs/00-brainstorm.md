# 00 — Brainstorm & Plan

Working title: **Multi-Cloud Lending Pipeline**
Owner: Toghrul · Started: 2026-05-04

> Captures the design decisions from the kickoff conversation. Every later doc / PR should be traceable back to a line in this file. If a decision changes, edit here and link the PR that changed it.

---

## 1. Why this project

- Learn **AWS Redshift** end-to-end (Serverless, Spectrum, COPY, WLM, RA3 trade-offs) — the only major warehouse missing from the existing portfolio.
- Implement a real **ELT** flow: extract+load is dumb (raw → S3 → Iceberg), all logic lives in dbt.
- Build a **dual-warehouse** topology so Redshift vs Snowflake comparisons are concrete, not theoretical.
- End with **multi-cloud** (BigQuery via BigLake) and **streaming** (Kinesis/Kafka) so the project covers the full spectrum.
- Keep cost low: serverless everywhere it exists, stop everything between sessions.

## 2. Domain — Lending / Credit Risk

A consumer-lending platform. Source systems (all simulated in Lambda):

| Source                | Grain                   | Notes                                                |
|-----------------------|-------------------------|------------------------------------------------------|
| `loan_applications`   | one app per row         | applicant, requested amount/term, channel, decision  |
| `loan_decisions`      | one decision per app    | approved/declined, APR, limit, decision rules fired  |
| `loan_drawdowns`      | one drawdown per loan   | disbursed amount, date, account                      |
| `payments`            | one repayment event     | scheduled vs actual, principal/interest split        |
| `delinquencies`       | snapshot of late loans  | DPD bucket (1-30, 31-60, 61-90, 90+), as-of date    |
| `credit_bureau_pulls` | one pull per app        | bureau score, hard inquiry flag, returned tradelines |
| `customers`           | one row per applicant   | demographics, KYC status, employment                 |

**Why lending fits the goals:**
- Regulatory reporting (DPD buckets, IFRS9 staging) is a textbook **Redshift** workload.
- Slowly-changing customer attributes + immutable payment events → clean **Data Vault 2.0** fit.
- Credit scoring → natural **BigQuery ML** hook in Phase 10.
- Different from prior `fintech-data-vault` (digital-banking platform) and `banking-data-vault` (payment processing).

## 3. Decisions (locked at kickoff)

| Decision                          | Choice                                                | Reason                                                  |
|-----------------------------------|-------------------------------------------------------|---------------------------------------------------------|
| Domain                            | Lending / Credit Risk                                 | New angle, regulatory reporting suits Redshift          |
| Warehouse #1 (primary)            | **Redshift Serverless**                               | Pay-per-RPU; no idle cost; real RS feature set          |
| Warehouse #2 (parallel)           | **Snowflake**                                         | Comparison + reuse existing account                     |
| Warehouse #3 (multi-cloud, late)  | **BigQuery** via BigLake                              | Reads same Iceberg tables; multi-cloud lakehouse        |
| Storage format                    | **Iceberg** on S3 (Glue Catalog)                      | Both warehouses can read; schema evolution; time-travel |
| Generators                        | **AWS Lambda** (Python 3.11)                          | Reuse pattern from `banking-data-vault-pipeline`        |
| Orchestration (early)             | **Self-hosted Airflow on EC2** (reuse iot-fleet box)  | $0 idle when stopped; same pattern already proven       |
| Orchestration (later)             | Optionally migrate to **MWAA** in a late phase        | Learn MWAA deliberately, not by default ($350/mo idle)  |
| Transform                         | **dbt** — ONE project, profile-dispatched             | Macro work via `adapter.dispatch` is a real skill       |
| Streaming (final phase)           | **Kinesis Data Streams + Firehose** + Snowpipe + RS streaming ingestion | AWS-native first; Kafka as stretch goal     |
| Python floor                      | **3.11**                                              | Avoid the 3.9 EOL warnings hit on link-tracker          |
| IaC                               | **Terraform** (or OpenTofu) per stack                 | Multi-cloud needs IaC; learn it properly                |
| Secrets                           | **AWS Secrets Manager**                               | Same pattern as other projects                          |
| Dependency management             | **uv**                                                | Fast, deterministic                                     |
| Docs                              | Numbered markdown in `docs/`                          | `00-brainstorm` → `01-aws` → ...                        |
| Karpathy principles               | `CLAUDE.md` at repo root                              | Think-before-code, simplicity, surgical changes, goal-driven |

## 4. Architecture (target end-state)

```
                ┌────────────────────────────────────────┐
                │  AWS Lambdas (7 generators)            │
                │  loan_apps · decisions · drawdowns     │
                │  payments · delinquencies · bureau     │
                │  customers                             │
                └─────────────────┬──────────────────────┘
                                  │ Parquet
                                  ▼
                       ┌────────────────────┐
                       │  S3 RAW            │
                       │  raw/<src>/        │
                       │  ingest_date=…/    │
                       └─────────┬──────────┘
                                 │ batch compaction → Iceberg
                                 ▼
                       ┌────────────────────┐
                       │  S3 ICEBERG        │
                       │  iceberg/lending/  │
                       │  Glue Catalog      │
                       └────┬──────────┬────┘
                            │          │
              ┌─────────────┘          └─────────────┐
              ▼                                      ▼
     ┌──────────────────┐                  ┌──────────────────┐
     │  Redshift        │                  │  Snowflake       │
     │  Serverless      │                  │                  │
     │  + Spectrum      │                  │  + Iceberg tbls  │
     └────────┬─────────┘                  └────────┬─────────┘
              │  dbt (redshift profile)             │  dbt (snowflake profile)
              ▼                                     ▼
       RAW → DV2 (hubs/links/sats) → MARTS (star) ←  same dbt project,
                                                     adapter.dispatch macros
              │                                     │
              └──────────────┬──────────────────────┘
                             │
                             ▼
                   ┌──────────────────────┐
                   │  Streamlit dashboard │
                   │  (compares both DWs) │
                   └──────────────────────┘

Late phases:
   S3 Iceberg ──(BigLake)──► BigQuery ──► BQ ML credit scoring
   Lambda ──► Kinesis ──► Firehose ──► S3 raw  (streaming variant)
                       └──► Redshift streaming ingestion
                       └──► Snowpipe Streaming
```

## 5. Phases — one PR per phase

| #   | Scope                                                                    | What you learn                                                                  |
|-----|--------------------------------------------------------------------------|---------------------------------------------------------------------------------|
| 0   | Repo scaffold, Python 3.11, `CLAUDE.md`, decision log, gitignore, README | Folder shape, karpathy principles, doc-first discipline                         |
| 1   | First Lambda (`loan_applications`) → S3 raw partitioned Parquet          | Lambda packaging, IAM execution role, S3 partition layout, Parquet schema       |
| 2   | All 7 generators + EventBridge schedule + dead-letter queue              | Multi-Lambda orchestration without Airflow, DLQs, observability                 |
| 3   | Iceberg tables on S3 + Glue Catalog, batch compaction job                | Iceberg metadata layout, manifests, snapshots, schema evolution                 |
| 4   | Redshift Serverless workgroup + IAM + external schema → Spectrum on Iceberg | RS Serverless setup, RPU sizing, COPY vs Spectrum, external schemas         |
| 5   | dbt-redshift: staging models → DV2 (hubs/links/sats) → star marts         | DV2 hash keys, multi-active sats, business vault, dbt incremental on RS         |
| 6   | Snowflake parallel: Iceberg-table integration, dbt-snowflake profile      | Snowflake Iceberg tables, COPY INTO comparison, storage integration              |
| 7   | dbt cross-warehouse refactor: `adapter.dispatch` macros, shared models    | Cross-database SQL, dispatch pattern, when to fork models                       |
| 8   | Self-hosted Airflow on EC2 (reuse iot-fleet box) orchestrating both        | DAG design, parallel branches, retries, S3KeySensor, dbt operator               |
| 9   | Streamlit dashboard reading BOTH warehouses, side-by-side metrics         | Connection abstraction, perf/cost comparison live                               |
| 10  | GCP: BigLake on Iceberg → BigQuery → BQ ML credit scoring                 | GCS/S3 interop, BigLake, BQ partitioning + clustering, BQ ML basics              |
| 11  | Streaming: Kinesis Data Streams + Firehose; RS streaming ingestion + Snowpipe Streaming | Streaming vs batch trade-offs, exactly-once, watermarks                |
| 12  | (stretch) Kafka via MSK Serverless instead of Kinesis                    | Kafka semantics, consumer groups, Schema Registry                               |
| 13  | Governance + cost retrospective: Lake Formation, RS WLM, SF masking, BQ row-level | When each warehouse wins; real $ numbers from your bill                |

Each phase = ONE PR with: code, doc page (`docs/0X-…md`), and a "what I learned" closing section.

## 6. Reuse from existing projects

To save build time, lift these patterns wholesale and adapt:

| From                              | What to reuse                                          |
|-----------------------------------|--------------------------------------------------------|
| `iot-fleet-monitor-pipeline`      | EC2 Airflow Docker Compose, ec2-setup.sh, swap config  |
| `banking-data-vault-pipeline`     | Lambda generator skeleton (PII masking + Parquet write); Iceberg via Glue Catalog |
| `fintech-data-vault-pipeline`     | dbt DV2 layout (hubs/links/sats naming, hash key macros) |
| `link-tracker-analysis`           | Streamlit page pattern, Snowflake conn helper, Secrets Manager namespacing |
| `airflow-orchestrator`            | Reusable DAG patterns, retry/backoff conventions       |

We do **not** copy code blindly — we extract patterns and re-implement against the new domain. Karpathy principle: surgical, no dead code.

## 7. Folder layout (created in Phase 0)

```
multi-cloud-lending-pipeline/
├── README.md
├── CLAUDE.md                         # karpathy principles
├── pyproject.toml                    # uv-managed
├── .python-version                   # 3.11
├── .gitignore
├── docs/
│   ├── 00-brainstorm.md              # this file
│   ├── 01-aws-foundations.md
│   ├── 02-iceberg-on-s3.md
│   ├── 03-redshift-deep-dive.md
│   ├── 04-data-vault-2.md
│   ├── 05-dbt-cross-warehouse.md
│   ├── 06-airflow-on-ec2.md
│   ├── 07-bigquery-multi-cloud.md
│   ├── 08-streaming-kinesis-kafka.md
│   └── 99-cost-retrospective.md
├── infra/
│   └── terraform/
│       ├── 00-state-backend/
│       ├── 01-iam/
│       ├── 02-s3/
│       ├── 03-glue-catalog/
│       ├── 04-redshift-serverless/
│       ├── 05-mwaa/                  # only if Phase 8b happens
│       └── 99-gcp/
├── lambdas/
│   ├── loan_application_generator/
│   ├── loan_decision_generator/
│   ├── loan_drawdown_generator/
│   ├── payment_generator/
│   ├── delinquency_generator/
│   ├── credit_bureau_generator/
│   ├── customer_generator/
│   └── shared/                       # masking, parquet writer, Faker setup
├── iceberg/
│   └── compaction_job/               # Glue or Spark on EMR Serverless
├── dbt/
│   ├── dbt_project.yml
│   ├── profiles/                     # redshift.yml + snowflake.yml + bigquery.yml
│   ├── macros/
│   │   ├── dispatch/                 # cross-warehouse helpers
│   │   └── data_vault/               # hash keys, hub/link/sat templates
│   └── models/
│       ├── staging/
│       ├── data_vault/
│       └── marts/
├── airflow/
│   └── dags/
├── streaming/                        # Phase 11+
│   ├── kinesis_producer/
│   └── consumers/
├── streamlit_app/                    # Phase 9
└── .github/workflows/                # dbt parse, terraform validate, ruff
```

## 8. Cost guardrails

| Resource                  | Idle cost                  | Mitigation                                                |
|---------------------------|----------------------------|-----------------------------------------------------------|
| Redshift Serverless       | $0 idle, ~$0.36/RPU-hr     | Set base capacity 8 RPU (min); auto-pause                 |
| Snowflake XS              | $0 after 60s auto-suspend  | Already configured                                        |
| EC2 (Airflow)             | ~$12/mo running, $0 stopped| Stop between sessions                                     |
| MWAA `mw1.small`          | **~$350/mo if left on**    | Skip until Phase 8b; tear down right after the lesson     |
| Kinesis Data Streams      | $0.015/shard-hr            | 1 shard, delete after streaming phase                     |
| MSK Serverless            | $0.75/hr cluster + traffic | Skip unless you really want Kafka; Kinesis is cheaper      |
| BigQuery                  | Free up to 1 TB scanned/mo | Use BQ partition+cluster pruning religiously              |

Set an **AWS Budget alarm at $50/month** before Phase 1 starts. Same for GCP.

## 9. Open items / parking lot

- Faker schema details (which fields per source) → settle in Phase 1.
- Whether to use **PyIceberg** vs Spark for compaction — decide in Phase 3.
- Whether streaming generator replaces batch Lambdas or runs in parallel — decide in Phase 11.
- CI: run dbt against Snowflake only (Redshift Serverless cold-start is too slow for PR checks)? Decide in Phase 5.

## 10. Definition of done (whole project)

- All 7 generators producing data on a schedule.
- Both Redshift and Snowflake serving the same DV2 + star schema.
- BigQuery serving the same Iceberg-backed marts via BigLake.
- Streamlit dashboard answering: "what's our delinquency curve, by channel, last 90d?" — with a side-by-side cost/perf panel for the three warehouses.
- A streaming variant of `payments` end-to-end (Kinesis → RS streaming + Snowpipe).
- `99-cost-retrospective.md` filled with real bills + lessons learned.

## 11. Next action

**Phase 0** = repo scaffold only. No cloud resources, no Lambda code. Just:
- `pyproject.toml`, `.python-version`, `.gitignore`, `README.md`
- `CLAUDE.md` with karpathy principles
- Empty folder skeleton above
- Initial `git init` + first commit

Wait for "go" before starting Phase 0.
