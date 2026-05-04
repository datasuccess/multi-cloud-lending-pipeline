# 01 — AWS Foundations & Phase 1 Plan

Goal of this phase: get **one** Lambda generator producing realistic `loan_applications` rows into S3 as partitioned Parquet, with a manually-deployed AWS footprint we can later codify in Terraform.

We deliberately stay narrow — **one source, one Lambda, no orchestration, no IaC, no Iceberg**. Phase 2 multiplies the generators; Phase 3 wraps Iceberg over the raw Parquet.

---

## 1. Definition of done

A green Phase 1 PR means:

**Functional**
- [ ] `lambdas/loan_application_generator/` contains a working handler.
- [ ] `lambdas/shared/` contains Parquet writer, Faker bootstrap, manifest writer, runs-ledger writer, structured-logging setup — reused by future generators.
- [ ] One manual `aws lambda invoke` produces: parquet + `.manifest.json` + `_SUCCESS` at the documented S3 prefix.
- [ ] The Parquet file opens cleanly in `pyarrow` and matches the declared schema.
- [ ] EventBridge rule fires the Lambda daily at 03:00 UTC.

**Monitoring** (see [`monitoring.md`](monitoring.md))
- [ ] Powertools `Logger` + `Metrics` wired in handler.
- [ ] At least 4 EMF metrics published (`rows_written`, `bytes_written`, `duration_ms`, `heartbeat`).
- [ ] Post-write validation reads parquet back and asserts shape; failure ⇒ no `_SUCCESS`, alarm fires.
- [ ] Pipeline runs ledger appends one JSONL line per invocation (success and failure).
- [ ] 3 CW alarms wired: Lambda errors, low volume, freshness.
- [ ] SNS topics `lending-alerts-p1-page` and `lending-alerts-p2-email` exist; email subscriptions confirmed.
- [ ] Runbook entries exist for all 3 alarms in `lambdas/loan_application_generator/README.md`.
- [ ] Manual chaos test: induce a validation failure → P1 alarm fires within 5 min.

**Security** (see [`pii-handling.md`](pii-handling.md))
- [ ] S3 bucket SSE-KMS via `alias/lending-pii`, bucket-key on, public access blocked.
- [ ] Generator IAM role: `kms:Encrypt` only, scoped to its prefix.
- [ ] `pii-loader-role` and `pii-investigator-role` defined (unused this phase).
- [ ] CloudTrail data events enabled on the KMS key and S3 bucket.

**Docs**
- [ ] `docs/01-aws-foundations.md` (this file) finalised with the *real* names/ARNs we created.
- [ ] Cost so far stays under **$2**.

## 2. Source data — `loan_applications`

One row per submitted application. **Immutable** at this stage — corrections come as new rows in later sources (`loan_decisions`, etc.). Schema includes realistic PII so we can practise a real masking + unmasking workflow.

### 2.1 Columns

PII column = highlighted in **PII type** column (DI=direct identifier, QI=quasi-identifier, blank=non-PII). Faker generates synthetic but realistic-looking values; SSNs / cards use Faker's test-range generators that can't collide with real values.

| Column                  | Type            | PII type | Notes                                                              |
|-------------------------|-----------------|----------|--------------------------------------------------------------------|
| `application_id`        | `string` (UUID) |          | Source primary key.                                                |
| `customer_id`           | `string` (UUID) |          | Pseudonymous; FK to `customers` (Phase 2).                         |
| `applied_at`            | `timestamp[us]` |          | UTC. Distributed across the 24h preceding the run, business-hours skew. |
| `first_name`            | `string`        | **DI**   | Faker `first_name()`.                                              |
| `last_name`             | `string`        | **DI**   | Faker `last_name()`.                                               |
| `email`                 | `string`        | **DI**   | Faker `email()`.                                                   |
| `phone`                 | `string`        | **DI**   | Faker `phone_number()`, E.164-ish.                                 |
| `date_of_birth`         | `date32`        | **QI**   | 18–80 range; quasi because age + zip can re-identify.              |
| `ssn`                   | `string`        | **DI**   | Faker `ssn()` — uses test ranges (9XX-XX-XXXX) by design.          |
| `gov_id_type`           | `string`        |          | enum: drivers_license, passport, state_id.                         |
| `gov_id_number`         | `string`        | **DI**   | Faker; format depends on `gov_id_type`.                            |
| `street_address`        | `string`        | **DI**   | Faker `street_address()`.                                          |
| `city`                  | `string`        | **DI**   | Faker `city()`.                                                    |
| `state`                 | `string`        | **QI**   | US state code.                                                     |
| `zip`                   | `string`        | **QI**   | Faker `zipcode()`.                                                 |
| `country`               | `string`        | **QI**   | ISO-2; mostly US, ~10% non-US.                                     |
| `ip_address`            | `string`        | **DI**   | Faker IPv4. Counts as PII under GDPR.                              |
| `user_agent`            | `string`        | **QI**   | Quasi via device-fingerprinting risk.                              |
| `amount_requested`      | `decimal(12,2)` |          | $1k–$50k, log-normal distribution.                                 |
| `term_months`           | `int8`          |          | One of {12, 24, 36, 48, 60}.                                       |
| `purpose`               | `string`        |          | enum: debt_consolidation, home_improvement, auto, medical, other.  |
| `channel`               | `string`        |          | enum: web, mobile, branch, partner.                                |
| `employment_status`     | `string`        |          | enum: employed, self_employed, unemployed, retired.                |
| `annual_income`         | `decimal(12,2)` |          | Conditional on `employment_status`.                                |
| `existing_debt`         | `decimal(12,2)` |          | 0 to 5× annual income, biased low.                                 |
| `referrer_source`       | `string`        |          | utm_source-like; null ~30%.                                        |
| `declared_purpose_text` | `string`        |          | Free-text; null ~50%. May contain accidental PII — flagged.        |
| `status`                | `string`        |          | At submission always "submitted". Later sources mutate.            |
| `_generator_version`    | `string`        |          | e.g. `"loan_app/0.1.0"`.                                           |
| `_ingest_at`            | `timestamp[us]` |          | When the Lambda wrote the file.                                    |

**Why explicit decimals not floats?** Money should never round. Redshift, Snowflake, and BigQuery all map `decimal` predictably; floats give you `0.1 + 0.2` surprises in marts.

**Why store unmasked PII at all?** Because real lending operations need it: KYC, fraud investigation, regulatory reporting, customer support. The right pattern is **encrypt + mask + audit + workflow-gated unmasking** — not "don't collect it." The full strategy lives in [`docs/pii-handling.md`](pii-handling.md). Phase 1 establishes the **storage** half (KMS, role split); Phase 4 wires the **masking** half in the warehouse.

## 3. AWS resources (manual now, Terraform-equivalent shown for Phase 2 lift)

Phase 1 creates these via the AWS CLI/console for hands-on learning. Each row includes the equivalent Terraform block so Phase 2's "lift to IaC" is mechanical — same names, same parameters, just declarative. Once Terraformed, the same module deploys to dev/staging/prod by changing `var.environment`.

| Resource              | Name                                              | Purpose                                                 |
|-----------------------|---------------------------------------------------|---------------------------------------------------------|
| **KMS key**           | alias `alias/lending-pii`                         | Encrypts S3 raw, future Snowflake/Redshift Iceberg PII fields. |
| S3 bucket             | `lending-raw-<account-id>` (us-east-1)            | Lake. SSE-KMS (`alias/lending-pii`). Versioning off.   |
| S3 bucket policy      | inline                                            | Deny non-TLS, deny non-KMS writes.                      |
| Lambda function       | `lending-loan-app-generator`                      | Generates a daily batch, writes one Parquet object.    |
| IAM role              | `lending-loan-app-generator-role`                 | Lambda execution role.                                  |
| IAM policy (inline)   | `lending-loan-app-generator-policy`               | `s3:PutObject` + `kms:GenerateDataKey` on its prefix.   |
| **IAM role**          | `lending-pii-loader-role`                         | For the future warehouse loader (Snowflake/RS service). Read raw + decrypt KMS. Created now, used Phase 4. |
| **IAM role**          | `lending-pii-investigator-role`                   | For audited human access. Read raw + decrypt KMS. **Assume only via MFA + time-bounded STS session.** Created now, used Phase 4. |
| Lambda layer          | `lending-pyarrow-layer` (Python 3.11, ARM64)      | Bundles `pyarrow`, `faker`, `aws_lambda_powertools`.    |
| EventBridge rule      | `lending-loan-app-daily`                          | `cron(0 3 * * ? *)` — daily at 03:00 UTC.               |
| CloudWatch log group  | `/aws/lambda/lending-loan-app-generator`          | Retention 7 days.                                       |
| CloudTrail data event | KMS decrypt + S3 GetObject on raw                 | Audit trail for any PII unmasking. Required for Phase 4 workflow but the trail must start *now* — auditors care about gaps. |
| AWS Budget alarm      | `lending-monthly-50usd`                           | Email at $50/mo project total.                          |
| **SNS topic**         | `lending-alerts-p1-page`                          | High-severity alerts (Lambda errors, freshness, schema drift). Email subscription. |
| **SNS topic**         | `lending-alerts-p2-email`                         | Low-volume warnings, cost warnings.                     |
| **CW alarm**          | `lending-loan-app-errors`                         | `Errors >= 1` in 5 min → P1.                            |
| **CW alarm**          | `lending-loan-app-freshness`                      | Custom metric `last_success_age_hours > 25` → P1.       |
| **CW alarm**          | `lending-loan-app-low-volume`                     | Custom metric `rows_written < 10000` → P2.              |
| **CW dashboard**      | `lending-pipeline`                                | One widget per source. Phase 1 fills loan_applications panel only. |

### 3.1 IAM policy — generator (least-privilege)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::lending-raw-<account-id>/raw/loan_applications/*"
    },
    { "Effect": "Allow",
      "Action": ["kms:GenerateDataKey", "kms:Encrypt"],
      "Resource": "arn:aws:kms:us-east-1:<account-id>:key/<key-id>"
    },
    { "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:<account-id>:log-group:/aws/lambda/lending-loan-app-generator:*"
    }
  ]
}
```

Note: the generator gets **encrypt-only** on KMS, never `kms:Decrypt`. It can write but cannot read back what it wrote.

### 3.2 Terraform equivalents (reference — applied in Phase 2)

```hcl
# infra/terraform/02-s3/main.tf  (Phase 2)
resource "aws_kms_key" "lending_pii" {
  description             = "Lending project PII envelope key"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.common_tags
}

resource "aws_kms_alias" "lending_pii" {
  name          = "alias/lending-pii"
  target_key_id = aws_kms_key.lending_pii.id
}

resource "aws_s3_bucket" "raw" {
  bucket = "lending-raw-${data.aws_caller_identity.current.account_id}"
  tags   = local.common_tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.lending_pii.arn
    }
    bucket_key_enabled = true   # cuts KMS API calls ~99%
  }
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "deny_insecure" {
  bucket = aws_s3_bucket.raw.id
  policy = data.aws_iam_policy_document.deny_insecure.json
}

# infra/terraform/04-lambda/main.tf  (Phase 2)
resource "aws_lambda_function" "loan_app_generator" {
  function_name                  = "lending-loan-app-generator"
  role                           = aws_iam_role.generator.arn
  package_type                   = "Zip"
  filename                       = "${path.module}/build/function.zip"
  handler                        = "handler.lambda_handler"
  runtime                        = "python3.11"
  architectures                  = ["arm64"]
  memory_size                    = 512
  timeout                        = 60
  reserved_concurrent_executions = 2
  layers                         = [aws_lambda_layer_version.pyarrow.arn]
  environment {
    variables = {
      RAW_BUCKET     = aws_s3_bucket.raw.id
      LOG_LEVEL      = "INFO"
      POWERTOOLS_SERVICE_NAME = "loan-app-generator"
    }
  }
  tags = local.common_tags
}

resource "aws_cloudwatch_event_rule" "loan_app_daily" {
  name                = "lending-loan-app-daily"
  schedule_expression = "cron(0 3 * * ? *)"
}
```

The `local.common_tags` map (`Project=lending`, `ManagedBy=terraform`, etc.) lives in `infra/terraform/00-state-backend/locals.tf`.

### 3.3 Schedule

`cron(0 3 * * ? *)` → fires once per day at **03:00 UTC**. Each invocation:
1. Generates **12,000** rows (= 500/hr × 24h) with `applied_at` distributed across the previous 24 hours, business-hours skewed (peak 10:00–18:00 local US time, dimmer at nights/weekends).
2. Writes one Parquet file to `ingest_date=YYYY-MM-DD/`, where `YYYY-MM-DD` = the date of the run (UTC).
3. Logs structured JSON: `{rows, bytes, s3_key, duration_ms, run_id}`.

## 4. Lambda packaging — choice

Three options:

| Option                           | When                                                  | Verdict for Phase 1                |
|----------------------------------|-------------------------------------------------------|------------------------------------|
| **Plain zip**                    | Pure stdlib, no native deps                            | No — we need pyarrow                |
| **Zip + Lambda layer**           | A few well-isolated deps                               | **Yes** — layer holds pyarrow/faker |
| **Container image (up to 10 GB)**| Heavy ML deps, custom OS packages, large bundles       | Overkill                            |

`pyarrow` is large (~70 MB). The 250 MB unzipped Lambda limit *includes* layers, so we keep the **function code <5 MB** and put pyarrow + faker in a **single shared layer** that all 7 future generators reuse.

Layer build (Phase 1 documents the recipe; Phase 2 automates):

```bash
# from lambdas/shared/
docker run --rm -v "$PWD":/var/task --entrypoint /bin/bash \
  public.ecr.aws/sam/build-python3.11:latest-arm64 -c "
    pip install --target /var/task/python pyarrow==16.* faker==25.*
  "
zip -r9 lending-pyarrow-layer.zip python
aws lambda publish-layer-version \
  --layer-name lending-pyarrow-layer \
  --compatible-runtimes python3.11 \
  --compatible-architectures arm64 \
  --zip-file fileb://lending-pyarrow-layer.zip
```

**ARM64** because ARM Lambdas are ~20% cheaper and pyarrow ships ARM wheels.

## 5. Code shape

```
lambdas/
├── loan_application_generator/
│   ├── handler.py             # entry point
│   ├── generator.py           # row generation logic
│   └── README.md              # how to invoke locally + deploy
└── shared/
    ├── __init__.py
    ├── parquet_writer.py      # Pandas→Arrow→S3 helper
    ├── partitioning.py        # ingest_date prefix builder
    └── faker_setup.py         # seed, US-locale providers
```

Handler contract:

```python
# handler.py (sketch — actual code in the PR)
def lambda_handler(event, context):
    """
    event keys (all optional):
      - rows:         int    default 500
      - seed:         int    default None (random)
      - ingest_date:  "YYYY-MM-DD"  default today UTC (override for backfills)
    Returns: {"s3_key": "...", "rows": N, "bytes": M}
    """
```

This contract lets us **backfill** by invoking with a past `ingest_date` — Phase 1 already prepares the door we'll walk through later.

## 6. S3 layout

```
s3://lending-raw-<account-id>/
├── raw/
│   └── loan_applications/
│       └── ingest_date=2026-05-04/                                # Hive-style partition
│           ├── 2026-05-04T03-00-00Z_<uuid>.parquet                # data
│           ├── 2026-05-04T03-00-00Z_<uuid>.parquet.manifest.json  # sidecar (rows, bytes, schema_hash, …)
│           └── _SUCCESS                                            # written LAST, atomic
└── _pipeline_runs/
    └── source=loan_applications/
        └── year=2026/month=05/day=04/
            └── run-<run_id>.jsonl                                  # one append per invocation
```

The `_SUCCESS` marker + manifest sidecar pattern is documented in [`monitoring.md`](monitoring.md) §5.1; the runs ledger in §5.2. Downstream loaders (Snowflake `COPY`, Spectrum) **wait for `_SUCCESS`** — never read raw parquet without the marker.

Why Hive-style? Athena, Spectrum, BigLake, and Snowflake all auto-discover Hive partitions. We get partition pruning for free in every warehouse.

Why `ingest_date` not `applied_date`? Two reasons:
1. **Append-only writes.** A late event still goes into the partition for the day we received it. Replays = DELETE-by-prefix + re-COPY.
2. **Decouples physical from logical.** Marts derive `applied_date` from the column, not the path.

## 7. Local testing

Two tiers:

**Tier 1 — pure unit (fast, no AWS):**

```python
# test_generator.py
def test_generator_produces_n_rows():
    rows = generator.make_rows(n=10, seed=42)
    assert len(rows) == 10
    assert all(r["amount_requested"] >= 1000 for r in rows)
```

**Tier 2 — round-trip Parquet (still no AWS):**

```python
def test_writer_round_trip(tmp_path):
    rows = generator.make_rows(n=5, seed=42)
    path = parquet_writer.write_local(rows, tmp_path / "out.parquet")
    table = pq.read_table(path)
    assert table.num_rows == 5
    assert "application_id" in table.schema.names
```

We **do not** spin up moto or LocalStack for Phase 1 — the S3 put is two lines, and we'll test it with a real `aws lambda invoke` against the dev bucket.

## 8. Step-by-step build order

The PR will land these in order so each step is independently verifiable:

1. `lambdas/shared/faker_setup.py` + unit test.
2. `lambdas/loan_application_generator/generator.py` (in-memory row generation) + unit tests.
3. `lambdas/shared/parquet_writer.py` (declared schema; write local first, then S3) + unit test.
4. `lambdas/shared/manifest.py` (manifest builder, schema_hash helper, `_SUCCESS` writer) + unit test.
5. `lambdas/shared/runs_ledger.py` (JSONL appender) + unit test.
6. `lambdas/shared/observability.py` (Powertools Logger + Metrics setup, common dimensions).
7. `lambdas/loan_application_generator/handler.py` — wires generator → writer → validator → manifest → success → ledger → metrics.
8. Local end-to-end test (writing to a temp dir): every artefact lands in correct order; deliberately corrupt the parquet → assert no `_SUCCESS`.
9. **Manual AWS provisioning** per §3 — KMS key, bucket+policy, IAM roles (3), Lambda fn + layer, SNS topics + email subs, CW alarms, dashboard. ARNs captured in this doc.
10. Build + publish the layer (§4 recipe).
11. `aws lambda update-function-code --zip-file fileb://function.zip`.
12. `aws lambda invoke --function-name lending-loan-app-generator out.json` → expect 200 + parquet + manifest + `_SUCCESS` in S3 + JSONL line in `_pipeline_runs/`.
13. **Chaos test**: deploy a build with a deliberate validation failure → confirm P1 alarm fires within 5 min, then revert.
14. Create EventBridge schedule, verify next firing.
15. **Backfill**: invoke for the last 14 days (`{"ingest_date":"YYYY-MM-DD"}` payload).
16. Runbook entries for all 3 alarms in `lambdas/loan_application_generator/README.md`.
17. Finalise this doc with real names/ARNs/timestamps. **PR ready for review.**

## 9. What we are NOT doing this phase

So we don't get pulled sideways:

- **No DLQ** — comes with multi-Lambda Phase 2.
- **No Terraform module** — manual via CLI; .tf blocks documented inline; Phase 2 lifts into a real module.
- **No Iceberg** — Phase 3.
- **No Snowflake / Redshift loads** — Phases 4–6.
- **No anomaly-detection alarms** — only static-threshold and freshness alarms now; ML-based drift in Phase 8.
- **No Lambda Insights extension** — defer to Phase 2 once we have multiple Lambdas to compare.
- **No Slack/PagerDuty integration** — email-only on SNS now; Slack in Phase 8.
- **No synthetic canary** — Phase 8.
- **No schema registry** — schema lives in `parquet_writer.py` for now; Glue Catalog in Phase 3.
- **No warehouse-side masking policies** — Phase 4 (the role *names* are reserved now though).

## 10. Cost estimate

| Item                                          | Monthly estimate                                   |
|-----------------------------------------------|----------------------------------------------------|
| Lambda invocations (1/day, ARM64, ~5s, 512MB) | 30/mo × 5s × 512 MB ≈ **<$0.01**                   |
| S3 PUT (1/day)                                | 30/mo × $0.005/1k = **<$0.01**                     |
| S3 storage (~3 MB/day Parquet, 12k rows)      | ~100 MB after a month × $0.023/GB = **<$0.01**     |
| KMS key                                       | $1/mo + $0.03/10k requests; bucket-key cuts requests **~99%** → **~$1.05** |
| CloudWatch logs (7-day retention)             | ~5 MB/day × $0.50/GB = **<$0.01**                  |
| CloudTrail (data events on KMS+S3)            | Free for first trail, then $0.10/100k events → **<$0.10** |
| **Total Phase 1**                             | **~$1.20/month**                                   |

Set the AWS budget alarm at **$50/mo total project** *before* running anything.

## 11. Decisions resolved at kickoff

| # | Decision                | Choice                                                                                  |
|---|-------------------------|-----------------------------------------------------------------------------------------|
| 1 | AWS region              | `us-east-1`                                                                             |
| 2 | AWS account             | Same one used by `iot-fleet-monitor` (confirm at first `aws sts get-caller-identity`).  |
| 3 | Volume                  | **12,000 rows/day** = 500/hr × 24h, generated as ONE daily file.                         |
| 4 | Schedule                | EventBridge `cron(0 3 * * ? *)` — once daily at 03:00 UTC.                               |
| 5 | PII strategy            | Real PII columns + KMS encryption + role split now; warehouse masking + audit Phase 4.   |
| 6 | Terraform               | Manual CLI for Phase 1 (learning); lift to Terraform module in Phase 2 with no behavioural change. |
| 7 | Backfill                | At end of Phase 1, run a loop invoking the Lambda for the last **14 days** so dbt has history to model. |

## 12. Learning checkpoints

After Phase 1 you should be able to answer, without looking it up:

- What's the difference between a Lambda layer and a container image, and when each wins.
- Why ARM64 is the default for new Lambdas.
- Why we partition by `ingest_date` not by `applied_at`.
- Why Parquet + decimals is the right starting format for raw.
- What an "execution role" is and why it's separate from the function's IAM principal.
- How EventBridge scheduling differs from Lambda's own (deprecated) schedules.

---

## 13. Branch strategy (this project's git workflow)

We want practice with **merge commits**, so we run a Git Flow lite:

```
main          ← stable, every phase ends here as a tagged release
  ▲
  │ merge --no-ff (release)
dev           ← integration branch; all phases pile up here first
  ▲
  │ merge --no-ff (PR)
feature/phase-N-<short>
```

**Rules:**

- Every phase opens a `feature/phase-N-<slug>` branch *off `dev`*.
- PR target = `dev`. Merge with `--no-ff` (or "Create a merge commit" in GitHub UI) so the merge commit survives even when fast-forward is possible. That's the whole point of practising it.
- When a phase is fully done (code + doc + green tests), open a second PR `dev → main`, also `--no-ff`. Tag `phase-N-complete` on the resulting merge commit.
- `main` is never committed to directly — only via merge.

**Naming convention:**

| Phase | Branch                                              |
|-------|-----------------------------------------------------|
| 1     | `feature/phase-1-loan-app-generator`               |
| 2     | `feature/phase-2-multi-generator-eventbridge`      |
| 3     | `feature/phase-3-iceberg-on-s3`                    |
| 4     | `feature/phase-4-redshift-serverless`              |
| ...   | ...                                                 |

**Why `--no-ff`** even when not needed? Because the merge commit is the visible record that "this lump of work landed together." It's also what `git log --first-parent main` can use to show one line per phase later. Fast-forward erases that.

**Why no `release/*` branches?** Solo project, low traffic. We tag on `main` instead.

---

## 14. Critical points (the TL;DR you should memorise)

The non-negotiables of this phase. If a future change breaks one of these, push back.

- **Idempotency by partition.** Re-running the Lambda for the same `ingest_date` overwrites the partition's contents (DELETE-by-prefix → re-write), never produces duplicates downstream.
- **Write-time partitioning, not event-time.** Path uses `ingest_date=` (when we *received* the row), not `applied_date=`. Late events still land in today's partition; logical time comes from the column.
- **Schema is a contract, not an inference.** We declare an explicit `pyarrow.Schema`. We never let pandas guess. Wrong inference = silently broken decimals or null/string drift.
- **Decimals for money, always.** No `float`. No `double`. `decimal(12,2)`.
- **UTC everywhere.** Lambda env, generator, S3 timestamps, partition dates. Localisation is a presentation concern, not a storage one.
- **Generator version stamped in every row.** `_generator_version="loan_app/0.1.0"`. When we change the schema, we bump the version and downstream knows where the discontinuity is.
- **Least-privilege IAM.** The Lambda role can `s3:PutObject` on **only** its prefix. Not the whole bucket. Not other prefixes.
- **ARM64 + Python 3.11.** ~20% cheaper than x86, longer support window than 3.9/3.10.
- **Realistic distributions, not uniform random.** Log-normal incomes, weighted purposes, skewed business hours. If the data is too clean, the dashboards lie.
- **Backfill is a first-class capability.** Handler accepts `ingest_date` in the event payload from day one. We don't bolt it on later.
- **Cost alarm before resources.** Budget set to **$50/month** in AWS Budgets *before* any Lambda invocation runs.

## 15. Production practices — what we apply *now* vs *later*

What real teams do for a Lambda → S3 producer like this, and where we are on each axis. The "Now" column is what Phase 1 must include; the "Later" column links to the phase that adds it.

| Practice                                            | Now (Phase 1)                                | Later                                              |
|-----------------------------------------------------|----------------------------------------------|----------------------------------------------------|
| **Structured (JSON) logging**                       | ✅ `aws_lambda_powertools.Logger` from line 1 | —                                                  |
| **Correlation / request IDs in logs**              | ✅ from Powertools                           | —                                                  |
| **CloudWatch retention set explicitly**            | ✅ 7 days (saves $)                          | Phase 8: 30 days when we wire alarms               |
| **Resource tags for cost allocation**              | ✅ `Project=lending`, `Phase=1`, `Owner=…`   | —                                                  |
| **S3 default encryption**                           | ✅ **SSE-KMS** with `alias/lending-pii`, bucket-key on | —                                       |
| **S3 block-public-access**                         | ✅ All four blocks ON                         | —                                                  |
| **S3 versioning**                                  | ❌ off — raw is append-only by design         | Reconsider only if compliance demands it           |
| **S3 lifecycle (raw → IA → Glacier)**              | ❌ off — too little data to matter            | Phase 10 cost-retro: enable when raw > 1 GB        |
| **Least-privilege IAM (resource-level ARNs)**     | ✅ as in §3                                   | —                                                  |
| **Lambda concurrency limit**                       | ✅ reserved concurrency = 2 (anti-runaway)   | Phase 2: per-fn limits when we have 7              |
| **Lambda timeout + memory tuned**                  | ✅ 60s / 512 MB starting; observe p99        | Re-tune in Phase 2 with real numbers               |
| **Dead-letter queue (DLQ)**                        | ❌ no — single fn, single source              | Phase 2: SQS DLQ + redrive policy                  |
| **Idempotency keys for the producer**              | ✅ via `ingest_date` partition replay-safety  | Phase 11: streaming will need at-most-once / dedup |
| **Schema registry**                                | ❌ — schema lives in code                     | Phase 3: Glue Schema + Iceberg                     |
| **Versioned generator (`_generator_version`)**    | ✅                                            | —                                                  |
| **Reproducible Lambda layer build**                | ✅ Docker SAM-builder image (deterministic)  | Phase 2: CI-built artefact                         |
| **Secrets via AWS Secrets Manager, not env vars** | ✅ pattern established                        | Phase 4 onward as actual creds appear              |
| **Infrastructure as Code (Terraform)**             | ⚠️ manual CLI; .tf blocks documented inline   | **Phase 2: lift to Terraform module** (replays to dev/staging/prod) |
| **CI: lint + test on PR**                          | ✅ ruff + pytest GitHub Action                | Phase 5: + dbt parse                               |
| **Runbook for ops**                                 | ✅ `lambdas/loan_application_generator/README.md` | extended every phase                          |
| **Monitoring / alerts**                            | ✅ EMF metrics, 3 CW alarms, SNS P1/P2, manifest sidecar, runs ledger, post-write validation, runbooks | Phase 2: Lambda Insights, DLQ alarms, full dashboard ([`monitoring.md`](monitoring.md)) |
| **Distributed tracing (X-Ray)**                    | ❌                                            | Phase 11 (multi-hop streaming)                     |
| **Backfill tooling**                                | ✅ event-arg `ingest_date` accepts a date     | Phase 8: Airflow DAG with date-range param         |
| **PII handling**                                    | ✅ realistic PII columns + KMS + role split  | Phase 4: warehouse masking policies + audited unmask workflow ([`pii-handling.md`](pii-handling.md)) |

The pattern is deliberate: every "Later" item has a phase. Nothing is hand-waved. If something doesn't have a phase, we either decided we don't need it or it's an honest gap to surface.

## 16. Why "one source first" (the answer to the multi-table question)

Doing all 7 generators in Phase 1 looks faster but is slower in practice:

1. **End-to-end before deep-and-narrow.** With one source we prove generator → writer → S3 → schedule → verification. The whole rail. Adding 6 more is then mechanical.
2. **Pattern extraction.** `lambdas/shared/` only forms once we've actually written the code twice. Trying to design `shared/` for 7 sources before writing one = abstraction-without-evidence.
3. **Cost containment.** A bug that runs every minute on 7 generators costs 7× more.
4. **Review surface.** A 1-Lambda PR is reviewable in one sitting. A 7-Lambda PR is a stack of "looks fine I guess" review comments.

Phase 2 lands the other six in roughly an evening — they're 90% the same code with different schemas.

## 17. Glossary (one-liners for the terms in this doc)

- **Terraform** — HashiCorp's Infrastructure-as-Code tool. Declarative `.tf` files describe cloud resources; `terraform plan` shows the diff, `terraform apply` makes it real. Not the same as **Teradata** (a legacy MPP warehouse vendor — irrelevant to this project).
- **IaC (Infrastructure as Code)** — managing cloud resources through committed text files instead of console clicks.
- **Lambda layer** — a zip of dependencies separate from the function code, mountable into multiple functions.
- **EventBridge** — AWS's event bus + scheduler. Replaces the deprecated CloudWatch Events.
- **Hive-style partitioning** — `key=value/` directory naming so query engines auto-discover partitions. Universally understood by Athena, Spectrum, Snowflake external tables, BigLake.
- **MPP (Massively Parallel Processing)** — warehouse architecture where queries fan out across compute nodes. Redshift, Snowflake, BigQuery, Teradata all use MPP.
- **DLQ (Dead Letter Queue)** — where failed events go when the main consumer can't process them. Usually SQS for Lambda.
- **DPD (Days Past Due)** — bucketing late loans by how late: `1-30`, `31-60`, `61-90`, `90+`. Standard in lending dashboards.

## 18. Next action

1. Create branches: `dev` (off `main`) and `feature/phase-1-loan-app-generator` (off `dev`). Push both. ✅ done.
2. Open this doc as a draft PR for review. ✅ done — PR #1.
3. Wait for "go phase 1 code" before touching any Lambda code.
