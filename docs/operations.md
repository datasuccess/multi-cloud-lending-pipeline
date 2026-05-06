# Operating philosophy — fix forward vs tear down

Cross-phase doc. Companion to [`cost-control.md`](cost-control.md) (§11 anti-patterns)
and [`validation.md`](validation.md) (escalation policies). The `99-teardown.sh`
script in this repo is a learning-project pattern; this doc is what changes the
day Phase 4 lands real-shaped data.

> **Operating principle.** Production fixes the patient; it doesn't shoot
> them and grow a new one. Teardown is a learned habit from local dev
> (`docker-compose down && up`, `rm -rf node_modules`) that doesn't
> translate to systems with state.

## The stateful / stateless split

The split is what determines whether teardown is even a sensible default.

### Stateful (data, identity, audit) → fix forward, never tear down

S3 buckets, databases, KMS keys, IAM roles with established trust
relationships, CloudTrail, Snowflake warehouses with data, Iceberg tables.
**The data is the asset; the resource is just a container around it.** If
something is wrong:

- Migrate the data to a corrected resource alongside, swap consumers, retire
  the old one *empty*.
- Or fix the config in place — bucket policies, IAM policies, lifecycle
  rules, alarm thresholds are all editable. Teardown to "reset" is a tell
  that someone doesn't understand the diff.
- Roll forward through schema migrations, not by recreating the table.

### Stateless (compute, scheduling) → replace freely

Lambda code, EventBridge rules, container images, CloudWatch alarms (the
alarm config, not the metric history), dashboards, ECS tasks. These are
*cattle*. Blue/green deploy, immutable infrastructure, "redeploy from
scratch" is the normal answer. No data loss because there was never data
to begin with — just config that lives in git.

## What debugging actually looks like in production

1. **Read the alarm / log / metric.** What's actually failing?
2. **Reproduce in lower environment.** Staging or local with synthetic data —
   never poke prod first.
3. **Fix forward.** Code change → PR → review → CI → staging → prod with
   canary or blue/green.
4. **For config issues:** edit the IaC, plan, review the diff, apply.
5. **For data issues:** migrate or backfill in place. Quarantine bad rows
   (the [`validation.md`](validation.md) "quarantine" policy), don't delete
   the whole partition.
6. **Rollback if the fix made it worse:** deploy the previous artifact.
   Compute resources support this trivially because they're stateless.

## Legitimate teardown cases in production

The exceptions exist and they're explicit, documented, multi-stakeholder
decisions — not a script someone ran on Friday afternoon.

| Case | What it looks like | Why teardown is OK here |
|---|---|---|
| **End-of-life decommissioning** | Multi-month migration: data exported, consumers migrated, retention satisfied, runbook executed step by step | The asset has been moved; the container is empty |
| **Dev/staging hygiene** | Short-lived preview env spun up by CI, torn down when the PR closes | Teardown is the design — the data is throwaway by definition |
| **Ephemeral compute clusters** | Spark-on-EMR, Redshift Serverless workgroups, ECS tasks: provision → run → destroy | Data lives elsewhere (S3); only compute is torn down |
| **Disaster recovery drills** | Deliberately destroy a region/AZ to verify failover works | Planned, scheduled, with a recovery target measured against an SLO |

In every legitimate case, what's torn down is either *empty* (data has been
moved) or *stateless* (no data was ever there).

## Anti-pattern: teardown-as-debugging

"It's broken, let me delete and recreate it" is the local-dev habit that
does the most damage in prod. Recreating a stateful resource:

- Loses the data.
- Loses the audit log of what happened to it.
- Often breaks consumers that pinned the previous ARN / endpoint / key.
- Doesn't actually identify the root cause, so the bug recurs the next day
  on a clean resource.

The corollary: when you reach for teardown in prod, treat it as a signal
you don't yet understand the bug. Stop, instrument, reproduce in staging.
Teardown is the move *after* root cause is known and migration is the
correct remediation — not before.

## Why production teardown looks completely different

The Phase 1 `99-teardown.sh` script is a single shell file behind one
`type 'yes'` prompt. Production teardown of a stateful resource passes
through every gate below:

1. **Legal retention check.** Loan applications carry CCPA / GLBA / SOX
   retention (typically 7 years). CloudTrail is often 7 years too
   (compliance evidence). Deletion before retention expires is illegal,
   not just unwise.
2. **Legal hold.** If any account is in litigation hold, no deletes pass —
   even retention-expired ones. Hold lookup is the first gate.
3. **Buckets refuse deletion by configuration.**
   - **MFA Delete** on the bucket: even the AWS account root user must
     present a hardware MFA token to delete a noncurrent version.
   - **Object Lock in Compliance mode**: objects literally cannot be
     deleted until their retention timestamp passes — not by root, not by
     AWS Support. Used for WORM (write-once-read-many) regulated data.
   - **Bucket policies with explicit `Deny`** on `s3:DeleteBucket` /
     `s3:DeleteObject` keyed off `aws:PrincipalArn` so only a break-glass
     role can do it.
4. **Resources owned by IaC, not shell scripts.** Terraform / Pulumi / CDK
   with **`prevent_destroy = true`** on stateful resources. `terraform
   destroy` errors out; you must edit the config and get it through code
   review before destruction is even possible.
5. **KMS keys are never deleted, only disabled.** A deleted CMK makes every
   object encrypted under it permanently unreadable — including backups.
   Production rotates and disables old keys; deletion happens after
   multi-year evidence the key is unused, and even then through a
   change-management ticket.
6. **Buckets aren't recreated under the same name.** S3 bucket names are
   globally unique with a deletion → recreate cooldown. Production migrates
   data and leaves the old bucket as a tombstone, or replaces resources
   behind an alias (Route53, Glue catalog) so consumers never reference the
   bucket name directly.
7. **Backups are independent of the source.** Cross-account replication to
   a separate "archive" account that the engineering team can't even
   authenticate into. So even if someone runs the equivalent of
   `99-teardown.sh` in the prod account, the data still exists somewhere
   they can't touch.

## What production "teardown" actually means

A *decommissioning runbook*, not a script. Multi-week. Stakeholder sign-off,
downstream consumer migration, retention-policy lookup, legal hold check,
data-export-and-attest, then resource removal. The destructive commands
themselves are gated behind change tickets, peer review, and break-glass
IAM (separate session, separate MFA, time-boxed). Often the "teardown" is
just *redirecting traffic* and leaving the old infra running for the legal
retention period — explicit deletion only happens at year 7.

## What this means for the project

| Phase | Teardown stance |
|---|---|
| **Phase 1** | `99-teardown.sh` is fine. Synthetic data, learning project, every cost gate works against the script not for it. |
| **Phase 2** | Same. Streaming consumer adds Kinesis / MSK; data is still synthetic. |
| **Phase 4** | `99-teardown.sh` gets **deleted from the repo.** The first phase that touches data with realistic shape (loan-application records, even synthetic) is the cutover. Replaced with a per-resource decommissioning runbook. |
| **Phase 4+** | All new buckets get versioning + lifecycle (already), plus `prevent_destroy` in IaC, plus MFA Delete on the prod bucket. KMS keys disable-only. |

The `99-teardown.sh` disclaimer that should live at the top of the script
once Phase 4 is in flight:

```bash
# WARNING: this script deletes synthetic data only.
# Production deletes pass through:
#   1. Legal hold check (no deletes if any account on hold)
#   2. Retention policy lookup (per-record minimum retention satisfied?)
#   3. Cross-account backup verification (data exists in archive account)
#   4. Change ticket + peer approval
#   5. Break-glass role assumption (separate MFA)
#   6. Audit log entry (who, what, why, ticket #)
# This script does NONE of that. Use only on synthetic/learning data.
```

## The mental model

Prod is a hospital, not a dev box. You fix the patient; you don't shoot
them and grow a new one. The exceptions exist (organ donation, hospice
care) and they're explicit, documented, multi-stakeholder decisions — not
a script you ran on Friday afternoon.
