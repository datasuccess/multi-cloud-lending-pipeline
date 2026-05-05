# Phase 1 — provisioning runbook

Ordered, idempotent shell scripts that turn the design in
[`docs/01-aws-foundations.md`](../docs/01-aws-foundations.md) into a working
AWS footprint. Each script can be re-run safely; outputs are written to
`/tmp/lending-phase1-outputs.env` so later scripts pick them up.

> Account: `497162053528`  ·  Region: `us-east-1`  ·  Alerts: `datasuccess1@gmail.com`

## Prereqs

```bash
aws sts get-caller-identity                 # confirm account 497162053528
aws configure get region                    # confirm us-east-1
which pip3 zip rsync python3                # all must exist
chmod +x infra/*.sh infra/lambda/*.sh
```

## Run order

| # | Script | What it does | Cost added |
|---|---|---|---|
| 0 | `infra/00-setup-foundations.sh` | AWS Budget @ $50/mo · KMS key + alias `alias/lending-pii` (rotation on) · S3 bucket `lending-raw-<acct>` (SSE-KMS, bucket-key, PAB, deny-insecure policy) | KMS ~$1/mo |
| 1 | `infra/01-setup-iam.sh` | 3 IAM roles: generator (S3 PutObject + KMS Encrypt), pii-loader (Phase 4), pii-investigator (MFA-required, 4h max session) | $0 |
| 2 | `infra/02-setup-monitoring.sh` | SNS P1+P2 topics + email subs · 3 alarms (errors/freshness/low-volume) · CW dashboard · CloudTrail data events on KMS+S3 | CloudTrail ~$0.10/mo |
| 3 | `infra/lambda/build-layer.sh` | Builds `lending-pyarrow-layer` (pyarrow + faker + powertools, ARM64 manylinux wheels) and publishes a layer version | $0 (S3 storage of zip is negligible) |
| 4 | `infra/lambda/package-function.sh` | Zips just `lambdas/loan_application_generator/` + `lambdas/shared/` | $0 |
| 5 | `infra/lambda/deploy.sh` | create-or-update Lambda, set concurrency=2, attach layer, log retention 7d, EventBridge rule `cron(0 3 * * ? *)` | invoked: ~$0.01/mo |
| 6 | `infra/03-invoke-and-backfill.sh smoke` | One manual invocation against today's partition | ~$0 |
| 7 | `infra/03-invoke-and-backfill.sh verify` | Lists partitions + reads latest manifest + tail of runs ledger | $0 |
| 8 | `infra/04-chaos-test.sh` | Forces validation failure (rows=5) → confirms no `_SUCCESS` + alarm fires | $0 |
| 9 | `infra/03-invoke-and-backfill.sh backfill 14` | 14-day backfill so dbt has history in Phase 5 | ~$0 |

Total Phase 1 monthly: **~$1.20**.

## Step-by-step

```bash
# 1) FIRST TIME ONLY: confirm AWS identity + region.
aws sts get-caller-identity
aws configure get region    # us-east-1

# 2) Foundations.
./infra/00-setup-foundations.sh

# 3) IAM. (Sleeps 10s at the end so IAM propagates.)
./infra/01-setup-iam.sh

# 4) Monitoring. Two SNS confirmation emails will land — CONFIRM BOTH.
./infra/02-setup-monitoring.sh

# 5) Build the layer. ~30s on a Mac with manylinux wheels cached.
./infra/lambda/build-layer.sh

# 6) Package the function code.
./infra/lambda/package-function.sh

# 7) Deploy Lambda + EventBridge rule.
./infra/lambda/deploy.sh

# 8) Smoke test — one invocation against today's partition.
./infra/03-invoke-and-backfill.sh smoke

# 9) Verify the artefacts landed.
./infra/03-invoke-and-backfill.sh verify

# 10) Chaos test. Wait ~5 min, then confirm the errors alarm went to ALARM.
./infra/04-chaos-test.sh

#     After verifying, clean the chaos partition (commands printed by the script).

# 11) Backfill 14 days.
./infra/03-invoke-and-backfill.sh backfill 14
```

## What gets created (names you can grep for in the console)

```
KMS:           alias/lending-pii
S3 buckets:    lending-raw-<acct>          (data)
               lending-raw-<acct>-cloudtrail (audit)
IAM roles:     lending-loan-app-generator-role
               lending-pii-loader-role
               lending-pii-investigator-role  (MFA + 4h max session)
Lambda:        lending-loan-app-generator     (python3.11, arm64, 512 MB, concurrency=2)
Layer:         lending-pyarrow-layer
EventBridge:   lending-loan-app-daily         cron(0 3 * * ? *)
SNS topics:    lending-alerts-p1-page
               lending-alerts-p2-email
Alarms:        lending-loan-app-errors        (P1, AWS/Lambda Errors)
               lending-loan-app-freshness     (P1, missing heartbeat 25h)
               lending-loan-app-low-volume    (P2, rows_written < 10000)
Dashboard:     lending-pipeline
CloudTrail:    lending-pii-data-events
Budget:        lending-monthly-50usd
```

## Re-running things

- **Code change only**: `package-function.sh && lambda/deploy.sh` (the deploy
  script does both update-code and update-config).
- **New layer version** (e.g. bumped pyarrow): `build-layer.sh && lambda/deploy.sh`.
- **Alarm tuning**: edit `02-setup-monitoring.sh` and re-run — `put-metric-alarm`
  is upsert.

## Teardown

```bash
./infra/99-teardown.sh
```

The KMS key itself isn't auto-deleted (AWS forces a 7-30 day pending-deletion
window). The script prints the one extra command to schedule it if you want
the key gone too.

## Failure modes already seen

- **`AccessDenied` on first Lambda invoke** — IAM hadn't propagated. The IAM
  script sleeps 10s at the end; if you skipped it, retry the invoke after 10s.
- **`InvalidPermission` on EventBridge add-permission** — already granted.
  The deploy script swallows that error.
- **Layer too large** — pyarrow is ~70 MB. The build script strips
  `__pycache__`, `tests/`, `.pyc` files. If you hit the 250 MB unzipped limit,
  pin pyarrow to 16.x or move to a container image (Phase 2 considers this).
- **`PutObject` denied with KMS error after deploy** — the bucket policy
  rejects writes without `aws:kms` SSE. Lambda and S3 default-encryption
  handle this automatically; client code never has to set the header.

## Phase 2 lift to Terraform

Every resource has an HCL equivalent shown inline in
[`docs/01-aws-foundations.md`](../docs/01-aws-foundations.md) §3.2. Phase 2's
job is to import these resources into a `terraform` module under
`infra/terraform/02-foundations/` and re-apply with `var.environment="dev"`.
No behavioural change.
