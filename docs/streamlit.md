# Streamlit monitoring app

A local-first multi-page app that watches the pipeline. New pages land per phase — Phase 1 ships three. Code lives under `streamlit_app/`, runtime details in [`streamlit_app/README.md`](../streamlit_app/README.md).

## Why local

Streamlit Cloud / EC2 hosting is deferred to Phase 8. Today the app runs on the developer's laptop with their own AWS creds, which keeps Phase 1 cost at $0 for the UI and avoids a public surface that has to be IAM-protected. The configuration is already in Secrets Manager, so promoting to a hosted environment in Phase 8 is a config switch, not a rewrite.

## Pages

| Page | Source | Phase added |
|---|---|---|
| `Home` | CloudWatch alarm states + S3 partition listing + runs ledger | 1 |
| `🩺 Pipeline Health` | Runs ledger sliced by day/hour, status mix, duration scatter | 1 |
| `📊 Lending KPIs` | Latest successful parquet read directly from S3 | 1 |
| `🔍 PII access audit` *(Phase 4)* | CloudTrail S3 data events + KMS management events | 4 |
| `❄️ Snowflake load health` *(Phase 4)* | Snowflake `INFORMATION_SCHEMA.LOAD_HISTORY` | 4 |
| `📈 dbt freshness` *(Phase 5)* | dbt `sources.yml` freshness checks | 5 |

## Production-grade KPI choices

The Phase 1 KPIs are slice cuts a credit / capital team would actually demand on day one of a new application source:

- **Volume + amount distribution** — total requested capital and the median + P95 amount, the basics of "how much exposure are we taking on per day".
- **DTI bands** — existing_debt / annual_income bucketed against the CFPB Qualified Mortgage 43% threshold. The share of apps above 43% is a leading indicator of underwriting drift.
- **Channel + purpose + employment + term mix** — how the funnel is shaped. Sudden swings are a quality / acquisition signal.
- **Geographic concentration** — top-10 US states, with concentration share. A new state suddenly reaching the top three is an audit prompt.
- **Unemployed share, non-US share** — population checks that catch generator drift early.

Decision / approval / loss metrics intentionally don't appear yet — `loan_applications` only has `status="submitted"` at this stage. Those land in a Phase-3 dbt mart that joins applications with `loan_decisions`, where they belong.

## Caching

- `st.session_state` holds the AppConfig (resolved once per browser session).
- `st.cache_resource` wraps the boto3 clients.
- `st.cache_data(ttl=...)` wraps S3 reads (60-120s) and CloudWatch reads (30s) — short enough that "refresh" reflects reality, long enough that flipping pages doesn't re-download.

## Running it

See [`streamlit_app/README.md`](../streamlit_app/README.md) for the install + run commands and the offline (`LENDING_SECRETS_LOCAL=1`) workflow.
