# Multi-Cloud Lending Pipeline

End-to-end ELT pipeline for a consumer-lending platform, built to learn AWS Redshift data warehousing in depth and run the same models across **Redshift, Snowflake, and BigQuery** off a shared **Iceberg lakehouse** on S3.

## Status

Phase **1 (in progress)** — loan_applications Lambda generator + AWS Secrets Manager + Streamlit monitoring app + anomaly injection engine.

Run order: [`RUNBOOK.md`](RUNBOOK.md).

## Architecture (target)

```
Lambda (7 generators) → S3 raw → S3 Iceberg (Glue Catalog)
                                  ├─► Redshift Serverless (Spectrum + dbt)
                                  ├─► Snowflake (Iceberg tables + dbt)
                                  └─► BigQuery (BigLake + dbt)   [Phase 10]
Streaming variant: Lambda → Kinesis → Firehose → S3 + RS streaming + Snowpipe   [Phase 11]
Orchestration: Airflow on EC2 (reused from iot-fleet-monitor)                   [Phase 8]
```

Full design + every decision: [`docs/00-brainstorm.md`](docs/00-brainstorm.md).

## Phases

The project ships in numbered PRs. Each PR = one phase = code + a `docs/0X-…md` learning page.

See section 5 of the brainstorm for the full 13-phase roadmap.

## Conventions

- **Docs-first.** No code without a corresponding doc page in `docs/`.
- **One PR per phase.** Small, reviewable, traceable to a brainstorm decision.
- **Karpathy principles.** See [`CLAUDE.md`](CLAUDE.md).
- **Python 3.11+.** Pinned in `.python-version`.
- **Cost discipline.** Stop everything between sessions. Budget alarm at $50/month.

## Reuse map

Patterns lifted from existing `/practice` projects (not code-copied — re-implemented):

| From                            | What                                                  |
|---------------------------------|-------------------------------------------------------|
| `iot-fleet-monitor-pipeline`    | EC2 Airflow Docker Compose, ec2-setup.sh              |
| `banking-data-vault-pipeline`   | Lambda generator skeleton, Iceberg via Glue Catalog   |
| `fintech-data-vault-pipeline`   | dbt DV2 layout, hash-key macros                       |
| `link-tracker-analysis`         | Streamlit page pattern, Secrets Manager namespacing   |
