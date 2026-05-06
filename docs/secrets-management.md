# Secrets management

All long-lived credentials, connection strings, and config the Lambdas / Streamlit app need are stored in **AWS Secrets Manager**. We do not put them in environment variables, in `.env` files in the repo, or in Terraform `tfvars`.

## Naming convention

```
lending/<env>/<resource>
```

- `lending/dev/streamlit-config` — bucket, region, alarm names, Powertools namespace. Read by the Streamlit app on launch.
- `lending/dev/pii-investigator-creds` *(Phase 4)* — break-glass IAM creds.
- `lending/dev/snowflake-loader` *(Phase 4)* — service-account password for the Snowflake load.
- `lending/dev/redshift-iam-role` *(Phase 4)* — role ARN for `COPY` from S3.

The `<env>` segment is `dev` for now. Phase 6 introduces `prod` alongside.

This matches the convention used across the sibling `/practice` projects — see `practice/INFRASTRUCTURE_NOTES.md` §4. Sticking to it means a single IAM policy template can grant least-privilege read access by prefix (`lending/dev/*`).

## Payload shape

Every secret is a JSON object — no plain-string secrets. That keeps the helper one shape and lets us add fields without breaking callers.

Example (`lending/dev/streamlit-config`):

```json
{
  "raw_bucket": "lending-raw-497162053528",
  "region": "us-east-1",
  "lambda_name": "lending-loan-app-generator",
  "alarm_errors": "lending-loan-app-errors",
  "alarm_freshness": "lending-loan-app-freshness",
  "alarm_low_volume": "lending-loan-app-low-volume",
  "powertools_namespace": "Lending/Generators",
  "powertools_service": "loan-app-generator"
}
```

## Helper

`lambdas/shared/secrets.py` exposes one function:

```python
from lambdas.shared.secrets import get_secret

cfg = get_secret("lending/dev/streamlit-config")
print(cfg["raw_bucket"])
```

- Boto3 client is created lazily and cached (`functools.lru_cache`).
- Resolved values are cached per-process — Lambda invokes don't repay the round-trip on warm starts; Streamlit reuses the cached value across page renders.
- Local development: set `LENDING_SECRETS_LOCAL=1` and put a JSON stub at `.secrets/<name with / replaced by _>.json`. Useful for the Streamlit dev loop on a plane.

## Rotation

Phase 4 wires automatic rotation via Secrets Manager's Lambda hook for the database secrets. Streamlit-config is updated in place by re-running `infra/05-bootstrap-secrets.sh` (uses `put-secret-value`).

## What goes in env vars instead

Settings that are *operating mode* rather than *credentials*:

- `MODE`, `ROWS_PER_RUN`, `MIN_ROWS`, `ANOMALY_*_PROB` — set by `infra/06-set-mode.sh`.
- `RAW_BUCKET`, `RAW_BUCKET_URI` — needed in the cold-start path before any code runs.

These are visible in the Lambda console (no auth secret), and Lambda's update-function-configuration is the natural way to flip them.
