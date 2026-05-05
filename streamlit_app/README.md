# Streamlit monitoring app

A local, multi-page UI to watch the pipeline. New pages are added per phase:

| Page | What it shows | Phase |
|---|---|---|
| `Home` | Alarm states, latest partitions, runs ledger | 1 |
| `🩺 Pipeline Health` | Per-day success/failure, duration, row trends | 1 |
| `📊 Lending KPIs` | Volume / amount / DTI / channel / state mix | 1 |

Authentication / secrets come from AWS Secrets Manager
(`lending/<env>/streamlit-config`) — see [docs/secrets-management.md](../docs/secrets-management.md).

## Run locally

```bash
# 1. Install the streamlit extras (one-time).
uv pip install -e ".[dev,streamlit]"
# or:  pip install -e ".[dev,streamlit]"

# 2. Make sure your shell has AWS creds with read access to the raw bucket
#    + Secrets Manager + CloudWatch:
aws sts get-caller-identity

# 3. Boot the app — pages are auto-discovered from streamlit_app/pages/.
streamlit run streamlit_app/Home.py
```

## Run offline (no AWS calls)

Useful for UI iteration on a plane:

```bash
mkdir -p .secrets
cat > .secrets/lending_dev_streamlit-config.json <<JSON
{
  "raw_bucket": "lending-raw-stub",
  "region": "us-east-1",
  "lambda_name": "lending-loan-app-generator",
  "alarm_errors": "lending-loan-app-errors",
  "alarm_freshness": "lending-loan-app-freshness",
  "alarm_low_volume": "lending-loan-app-low-volume",
  "powertools_namespace": "Lending/Generators",
  "powertools_service": "loan-app-generator"
}
JSON

LENDING_SECRETS_LOCAL=1 streamlit run streamlit_app/Home.py
```

(The data pages will be empty without S3, but Home renders.)
