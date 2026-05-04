# 01 — AWS Foundations & Phase 1 Plan

Goal of this phase: get **one** Lambda generator producing realistic `loan_applications` rows into S3 as partitioned Parquet, with a manually-deployed AWS footprint we can later codify in Terraform.

We deliberately stay narrow — **one source, one Lambda, no orchestration, no IaC, no Iceberg**. Phase 2 multiplies the generators; Phase 3 wraps Iceberg over the raw Parquet.

---

## 1. Definition of done

A green Phase 1 PR means:

- [ ] `lambdas/loan_application_generator/` contains a working handler.
- [ ] `lambdas/shared/` contains the Parquet writer + Faker bootstrap reused by future generators.
- [ ] One manual `aws lambda invoke` produces a Parquet file at the documented S3 prefix.
- [ ] The Parquet file opens cleanly in `pyarrow` and matches the declared schema.
- [ ] An EventBridge rule fires the Lambda on a schedule (cron once/hour for now — easy to disable).
- [ ] `docs/01-aws-foundations.md` (this file) is finalised with the *real* names/ARNs we created.
- [ ] Cost so far stays under **$1**.

## 2. Source data — `loan_applications`

One row per submitted application. **Immutable** at this stage — corrections come as new rows in later sources (`loan_decisions`, etc.). Schema:

| Column                  | Type            | Notes                                                          |
|-------------------------|-----------------|----------------------------------------------------------------|
| `application_id`        | `string` (UUID) | Source primary key.                                            |
| `customer_id`           | `string` (UUID) | FK to `customers` (Phase 2).                                    |
| `applied_at`            | `timestamp[us]` | UTC. Generator skews to business hours but allows nights.       |
| `amount_requested`      | `decimal(12,2)` | $1k–$50k, log-normal distribution.                              |
| `term_months`           | `int8`          | One of {12, 24, 36, 48, 60}.                                    |
| `purpose`               | `string`        | enum: debt_consolidation, home_improvement, auto, medical, other. |
| `channel`               | `string`        | enum: web, mobile, branch, partner.                             |
| `employment_status`     | `string`        | enum: employed, self_employed, unemployed, retired.             |
| `annual_income`         | `decimal(12,2)` | Faker-generated, conditional on `employment_status`.            |
| `existing_debt`         | `decimal(12,2)` | 0 to 5x annual income, biased low.                              |
| `state`                 | `string`        | US state code.                                                  |
| `country`               | `string`        | ISO-2; mostly US, ~10% non-US to make geo dims interesting.      |
| `ip_address`            | `string`        | Faker IPv4.                                                     |
| `user_agent`            | `string`        | Faker UA. Used later for device classification.                 |
| `referrer_source`       | `string`        | utm_source-like; null ~30%.                                     |
| `declared_purpose_text` | `string`        | Free-text; null ~50%.                                           |
| `status`                | `string`        | At submission always "submitted". Later sources mutate.         |
| `_generator_version`    | `string`        | e.g. `"loan_app/0.1.0"`. Lets us evolve the schema cleanly.     |
| `_ingest_at`            | `timestamp[us]` | When the Lambda wrote the file.                                 |

**Why explicit decimals not floats?** Money should never round. Redshift, Snowflake, and BigQuery all map `decimal` predictably; floats give you `0.1 + 0.2` surprises in marts.

## 3. AWS resources (manual for now, Terraform in a later phase)

| Resource           | Name                                              | Purpose                                                 |
|--------------------|---------------------------------------------------|---------------------------------------------------------|
| S3 bucket          | `lending-raw-<account-id>` (us-east-1)            | Lake. Versioning **off** for now (raw is append-only).  |
| Lambda function    | `lending-loan-app-generator`                      | Generates a batch and writes one Parquet object.        |
| IAM role           | `lending-loan-app-generator-role`                 | Lambda execution role.                                  |
| IAM policy         | `lending-loan-app-generator-policy` (inline)      | `s3:PutObject` on the bucket prefix only.               |
| Lambda layer       | `lending-pyarrow-layer` (Python 3.11, ARM64)      | Bundles `pyarrow`, `faker`. Keeps fn code small.        |
| EventBridge rule   | `lending-loan-app-hourly`                         | `cron(0 * * * ? *)` — top of every hour.                |
| CloudWatch log grp | `/aws/lambda/lending-loan-app-generator`          | Default; retain 7 days.                                 |

We will **not** create yet: SQS DLQ, Lambda destinations, X-Ray, custom KMS key, VPC config. Phase 2 adds DLQ + observability.

### IAM policy (least-privilege starting point)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::lending-raw-<account-id>/raw/loan_applications/*"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:<account-id>:log-group:/aws/lambda/lending-loan-app-generator:*"
    }
  ]
}
```

Plus the AWS-managed `AWSLambdaBasicExecutionRole` for log-group creation.

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
└── raw/
    └── loan_applications/
        └── ingest_date=2026-05-04/                 # Hive-style partition
            └── 2026-05-04T13-00-00Z_<uuid>.parquet
```

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
3. `lambdas/shared/parquet_writer.py` (write to local fs first, then `s3://`) + unit test.
4. `lambdas/loan_application_generator/handler.py` glue.
5. **Manual AWS provisioning** following section 3, ARNs captured in this doc.
6. Build + publish the layer (section 4 recipe).
7. `aws lambda update-function-code --zip-file fileb://function.zip`.
8. `aws lambda invoke --function-name lending-loan-app-generator out.json` → expect 200 + S3 object exists.
9. Create the EventBridge schedule, verify the next firing.
10. Update this doc with real names/ARNs/timestamps. **PR ready.**

## 9. What we are NOT doing this phase

So we don't get pulled sideways:

- **No DLQ** — comes with multi-Lambda Phase 2.
- **No Terraform** — manual now; codified in a dedicated infra phase later. (Karpathy: simplicity first.)
- **No Iceberg** — Phase 3.
- **No Snowflake / Redshift loads** — Phases 4–6.
- **No alerting** — basic CloudWatch logs only.
- **No schema registry** — schema lives in `parquet_writer.py` for now; Glue Catalog in Phase 3.

## 10. Cost estimate

| Item                                  | Monthly estimate                                  |
|---------------------------------------|---------------------------------------------------|
| Lambda invocations (24/day, ARM64)    | ~720/mo × ~200 ms × 256 MB ≈ **<$0.01**           |
| S3 PUT (24/day)                       | ~720/mo × $0.005/1k = **<$0.01**                  |
| S3 storage (~10 MB/day Parquet)       | ~300 MB after a month × $0.023/GB = **<$0.01**    |
| CloudWatch logs (7-day retention)     | ~5 MB/day × $0.50/GB = **<$0.01**                 |
| **Total Phase 1**                     | **<$0.05/month**                                  |

Set the AWS budget alarm at **$50/mo total project** *before* running anything.

## 11. Open decisions (resolve in PR)

- AWS region: **us-east-1** (cheapest; same region as future BigQuery transfer source).
- Account: same one used for `iot-fleet-monitor`? Confirm at PR time.
- How many rows per invocation? Default **500/hour** for now. Tunable via event payload.
- Backfill plan: invoke loop over the last 14 days at PR time so dbt has data to chew on in later phases.

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

## 14. Next action

1. Create branches: `dev` (off `main`) and `feature/phase-1-loan-app-generator` (off `dev`). Push both.
2. Wait for "go phase 1 code" before touching any Lambda code.
