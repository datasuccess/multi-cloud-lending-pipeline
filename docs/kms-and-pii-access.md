# KMS, encryption, and PII access — the AWS layer

This doc covers the **infrastructure** side of PII protection: how data is
encrypted at rest, how access to the encrypted bytes is gated, and how the
audit trail is constructed. It complements [`pii-handling.md`](pii-handling.md),
which covers the **warehouse** side (column-level masking in
Snowflake/Redshift, the unmasking-request workflow, GDPR rights).

> **Two doors.** A user only sees raw PII when **both** doors open for their
> identity:
>
> 1. **AWS door** — KMS Decrypt + S3 GetObject on the encrypted parquet.
> 2. **Warehouse door** — Snowflake `PII_INVESTIGATOR` role granting unmasked
>    columns.
>
> This doc is door #1. `pii-handling.md` is door #2.

## 1. Why a customer-managed KMS key (CMK)

S3 server-side encryption has three flavors. We pick the strictest:

| Flavor | Who owns the key | Access boundary | CloudTrail audit | Key rotation |
|---|---|---|---|---|
| `SSE-S3` | AWS, opaque | none — anyone with `s3:GetObject` reads | bucket-level only | AWS-managed |
| `SSE-KMS` with `aws/s3` AWS-managed key | AWS, visible | one shared key per account — too coarse | yes, but every S3 op shows up — too noisy | AWS-managed |
| **`SSE-KMS` with our CMK** | **us** | **separate** — `s3:GetObject` is not enough; you also need `kms:Decrypt` on the CMK | yes, scoped to *our* key | our schedule |

The third gives us three things the others can't:

1. **A second access boundary.** S3 perms alone aren't enough. The PII
   investigator role you saw in `01-setup-iam.sh` works precisely because
   the trust + policy combination grants `kms:Decrypt`. A role with full
   bucket read but no KMS perms gets ciphertext it can't decrypt.
2. **Granular CloudTrail.** Every `kms:Decrypt` call is logged with
   requester ARN, key ARN, and encryption context. We can answer *"who
   unwrapped a data key for the loan_applications bucket on 2026-05-05?"*
   in CloudTrail directly.
3. **Independent rotation.** We control when (and whether) the CMK rotates.
   Annual rotation is enabled by default; we can pin a specific version when
   regulatory holds require it.

The CMK lives at `alias/lending-raw-cmk`. Its key policy (separate from IAM
policies) names the account root as administrator and the three lending
roles as users — covered in `00-setup-foundations.sh`.

## 2. Envelope encryption — what actually happens on PutObject

Most engineers picture KMS as "the thing that encrypts my parquet." That's
not what KMS does. The CMK never touches your data. KMS only encrypts
**small data keys**, and the data keys encrypt the data. This is *envelope
encryption*. The flow:

```
PutObject (write side):
  1. Lambda calls s3:PutObject with ServerSideEncryption=aws:kms
  2. S3 calls kms:GenerateDataKey on our CMK
     ← KMS returns { Plaintext: <data key>, CiphertextBlob: <wrapped key> }
  3. S3 encrypts the parquet bytes with the plaintext data key (AES-256-GCM)
  4. S3 stores: <encrypted parquet> + <wrapped data key in object metadata>
  5. S3 immediately discards the plaintext data key

GetObject (read side):
  6. Reader calls s3:GetObject
  7. S3 reads the wrapped data key from object metadata
  8. S3 calls kms:Decrypt on the wrapped key
     ← KMS returns the plaintext data key
  9. S3 decrypts the parquet, streams it to the reader
 10. Plaintext data key is again discarded
```

Key consequences:

- **The CMK never leaves KMS.** It's a hardware-backed key sitting in AWS's
  HSMs; you can't export it.
- **Every object gets its own data key.** Compromise of one decrypted data
  key compromises one parquet, not the whole bucket.
- **KMS scales because it only handles small ops.** The CMK encrypts a
  ~32-byte data key; AES-256-GCM on the data is done by S3's edge in
  microseconds. KMS handles ~10k requests/sec for tiny operations.
- **`kms:Decrypt` is the audit point.** Every read triggers a Decrypt. If
  you control Decrypt, you control reads — even if S3 perms are wide open.

## 3. The encrypt-only pattern

Look at the lambda's main inline policy in `01-setup-iam.sh`:

```json
{
  "Sid": "EncryptOnly",
  "Effect": "Allow",
  "Action": ["kms:GenerateDataKey", "kms:Encrypt"],
  "Resource": "${KMS_KEY_ARN}"
}
```

**No `kms:Decrypt`.** The lambda can write but cannot read its own output.

Why this matters: if the lambda is ever compromised — a vulnerable dep, a
malicious payload, an injected event — the blast radius is **write-only**.
An attacker controlling the lambda can write garbage but cannot exfiltrate
existing PII partitions.

A separate inline policy (`-readback`) grants `kms:Decrypt` *only* for the
post-write validation read of objects under `raw/loan_applications/*`:

```json
{
  "Sid": "DecryptOwnObjects",
  "Effect": "Allow",
  "Action": ["kms:Decrypt"],
  "Resource": "${KMS_KEY_ARN}"
}
```

So Decrypt is granted, but only for the round-trip verification step. The
narrowness is the point.

### Why two policies and not one

Both grants live on the *same* IAM role. We could combine them into a
single inline policy with `["kms:GenerateDataKey", "kms:Encrypt",
"kms:Decrypt"]`. We deliberately don't, because the two grants have
**different blast radii**:

| Scenario | Combined policy | Split policies |
|---|---|---|
| Lambda compromised, attacker writes garbage | bounded write-only (same) | bounded write-only (same) |
| Lambda compromised, attacker tries to exfiltrate **existing** PII partitions | **succeeds** — Decrypt granted in main policy with broad scope | **fails** — main policy has no Decrypt; readback policy is scoped narrowly |
| Phase 4 splits the validator into a separate Lambda | must edit + redeploy the combined policy carefully | delete one inline policy; main writer policy untouched |
| Security review reads the role's policies | one statement with all three actions, intent unclear | `-readback` policy advertises its purpose by name |

The split is also a **review affordance**. A security reviewer auditing the
role sees a policy literally named `…-policy-readback` and immediately
understands *why* Decrypt exists. A combined policy hides that distinction.

Phase 2 takes this further: once a separate read-back validator Lambda is
provisioned, we drop the readback policy from the generator role entirely.
The generator becomes truly write-only; a different role does the
validation. Phase 1 has one Lambda doing both jobs, so the seam is at the
**policy level** rather than the **role level** — but it's in the right
place for the future role split, with no code changes needed in the writer.

## 4. The three-role model

Three roles at the AWS layer, three doors, all gated on KMS:

| Role | Trust | KMS perms | S3 perms | Used by | Phase |
|---|---|---|---|---|---|
| `lending-loan-app-generator-role` | `lambda.amazonaws.com` | Encrypt + Decrypt own writes | Write `raw/loan_applications/*` + read-back | The lambda only | 1 |
| `lending-pii-loader-role` | account root → tightened to Snowflake/RS principal in Phase 4 | Decrypt + DescribeKey | Read `raw/*` | Snowflake/Redshift via STORAGE INTEGRATION | 4 |
| `lending-pii-investigator-role` | account root, **MFA required**, max 4h session | Decrypt + DescribeKey | Read `raw/*` | A human, ad-hoc | 1 (created), 4 (used) |

Every door logs to CloudTrail. The CloudTrail trail (`lending-audit-trail`,
provisioned in `02-setup-monitoring.sh`) captures:

- All **management events** — including every `kms:Decrypt`, `kms:Encrypt`,
  `kms:GenerateDataKey`, and every `sts:AssumeRole` against these roles.
- **Data events** on the raw bucket — every `s3:GetObject` on a PII parquet.

The result: any read of the encrypted PII parquets is reconstructable as
*who* (CallerIdentity), *when* (eventTime), *via what role* (sourceIPAddress
+ userIdentity.sessionContext), and *for which key* (resources.ARN).

## 4a. Why CloudTrail logs live in a separate bucket

CloudTrail's log files do **not** ship to the same bucket they're auditing.
That would be a circular dependency, and AWS forbids it. So we have two
buckets:

| Bucket | Purpose | Who writes | Who reads |
|---|---|---|---|
| `lending-raw-<acct>` | The actual data — parquet, manifests, `_SUCCESS`, runs ledger | Lambda IAM role | Phase 4 loaders, Streamlit, dbt sources |
| `lending-audit-<acct>` (separate, dedicated) | CloudTrail's own log files — JSON gzipped, ~5 min flush cadence | The CloudTrail service principal | Security / compliance only — read-rare, write-heavy |

Three reasons for the split:

1. **Tamper-resistance.** The audit bucket runs with stricter controls:
   object-lock in compliance mode (immutable for N years), a separate KMS
   key, and IAM scoped to *write-only* for the trail service. Even an
   account admin can't delete a CloudTrail log within the lock period. If
   the data bucket is compromised, the attacker cannot cover their tracks
   by editing the audit logs — those live behind a different lock.
2. **Different lifecycle.** Data bucket: 30-day noncurrent-version expiry,
   eventual Glacier tier. Audit bucket: 7-year retention for SOX-style
   financial-data compliance, no early expiry, no overwrites. You can't
   express both policies cleanly on a single bucket.
3. **Different access pattern.** The data bucket gets billions of reads
   (every dbt run, every analyst query). The audit bucket gets touched
   maybe once per incident. Mixing them would force the audit bucket into
   the same hot-path IAM that needs frequent updates — every IAM change to
   the data path becomes a security review on the audit path too.

The convention in mature AWS estates is one **audit account** + one
**audit bucket** that all production accounts ship to (the AWS Control
Tower / Landing Zone pattern). Phase 1 is single-account so we'd just have
a separate bucket in the same account; multi-account splits ship in
Phase 8.

## 5. Worked examples

### Example A — analyst investigating a fraud case

The "human path." The investigator role is the only one that can be
assumed by a person, and only with MFA.

```bash
# Step 1: the analyst is logged into AWS as their normal IAM user.
#         Their user has NO direct PII access — that's by design.

# Step 2: assume the investigator role with MFA.
aws sts assume-role \
  --role-arn arn:aws:iam::497162053528:role/lending-pii-investigator-role \
  --role-session-name fraud-case-2026-05-06-analyst-jdoe \
  --serial-number arn:aws:iam::497162053528:mfa/jdoe \
  --token-code 123456 \
  --duration-seconds 3600
# → returns short-lived AccessKeyId / SecretAccessKey / SessionToken

# Step 3: with those creds, read the partition.
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...
aws s3 cp s3://lending-raw-497162053528/raw/loan_applications/ingest_date=2026-05-05/<file>.parquet ./
# S3 returns ciphertext, KMS unwraps the data key (because the role has
# kms:Decrypt), bytes decrypt locally.
```

What's logged in CloudTrail (auditable forever):

| Event | What you can answer |
|---|---|
| `AssumeRole` (with MFA assertion) | "Who claimed the investigator role at 14:32?" |
| `kms:Decrypt` (one per object) | "Which CMK + which key version was used?" |
| `s3:GetObject` (data event) | "Which parquet did they pull?" |

If the trust policy didn't have `Bool: aws:MultiFactorAuthPresent=true`,
step 2 would succeed without MFA — and the audit trail would lose the
strongest assertion of "this was a human, present at the keyboard."
That MFA condition is non-negotiable.

### Example B — Snowflake loading a partition (Phase 4)

The "machine path." Snowflake assumes the loader role via a STORAGE
INTEGRATION; no human in the loop, no MFA.

```sql
-- One-time setup in Snowflake.
CREATE STORAGE INTEGRATION lending_raw_int
  TYPE = EXTERNAL_STAGE
  STORAGE_PROVIDER = 'S3'
  ENABLED = TRUE
  STORAGE_AWS_ROLE_ARN = 'arn:aws:iam::497162053528:role/lending-pii-loader-role'
  STORAGE_ALLOWED_LOCATIONS = ('s3://lending-raw-497162053528/raw/');
-- Snowflake reveals its IAM principal + external ID; we update the
-- loader role's trust policy to require both. (Phase 4 wiring.)

-- Per-partition load.
COPY INTO raw.loan_applications
  FROM @lending_raw_stage/ingest_date=2026-05-05/
  FILE_FORMAT = (TYPE = PARQUET)
  ON_ERROR = ABORT_STATEMENT;
```

What happens under the hood:

1. Snowflake's IAM principal calls `sts:AssumeRole` against
   `lending-pii-loader-role` with the agreed external ID. The trust policy
   accepts.
2. With those creds, Snowflake reads parquet objects under the partition.
   S3 ciphertext → KMS Decrypt (loader role has it) → plaintext parquet
   streamed into Snowflake.
3. CloudTrail logs the AssumeRole, every Decrypt, every GetObject, all
   tagged with the role session name Snowflake used.
4. Snowflake's `RAW.LOAN_APPLICATIONS` table now has the data. That's the
   handoff to door #2: warehouse-side masking takes over.

### Example C — what fails, and how cleanly it fails

A user with full S3 read on the bucket but **no** KMS Decrypt:

```bash
aws s3 cp s3://.../<file>.parquet ./
# fatal error: An error occurred (AccessDenied) when calling the GetObject
#   operation: User: ... is not authorized to perform: kms:Decrypt on
#   resource: arn:aws:kms:us-east-1:497162053528:key/...
```

The error message names KMS, not S3 — which is exactly what we want, because
that surfaces the *real* missing permission to whoever's troubleshooting.

A compromised lambda trying to read another partition:

```python
# Hypothetical attacker code running inside the lambda:
boto3.client("s3").get_object(Bucket="lending-raw-...", Key="raw/.../old.parquet")
# botocore.exceptions.ClientError: AccessDenied — kms:Decrypt
```

The encrypt-only pattern stops the read at KMS. S3 might return the
ciphertext bytes, but KMS refuses to unwrap the data key, so S3's
decryption step fails server-side and AccessDenied surfaces.

## 6. The compliance answers this design enables

A PII regulator typically asks four questions. The architecture above lets
us answer each in one query:

| Question | Where the answer lives |
|---|---|
| "Who has read PII in the last 90 days?" | CloudTrail filter: `eventName=Decrypt AND resources.ARN=<our CMK>` joined with the assumed-role identity |
| "Show me every read of subject X's data." | S3 data events filtered by `resources.ARN` containing the partition + Snowflake `ACCESS_HISTORY` filtered by row-level access |
| "Prove that engineers can't bypass the audit log." | Bucket policy denies non-TLS + non-SSE-KMS — meaning every read has to go through KMS, which is logged |
| "How do you revoke access?" | Remove the role from the CMK key policy — every in-flight Decrypt fails immediately, no app redeploy needed |

That last one is why CMK > AWS-managed key for PII. Revocation is one
JSON edit, takes effect in seconds, and is itself logged.

## 7. Where the bits live

| Concern | File |
|---|---|
| CMK creation + key policy | `infra/00-setup-foundations.sh` |
| Bucket policy (deny non-TLS, deny non-SSE-KMS) | `infra/00-setup-foundations.sh` |
| Default bucket encryption | `infra/00-setup-foundations.sh` |
| Three IAM roles + their inline policies | `infra/01-setup-iam.sh` |
| CloudTrail trail + management/data event selectors | `infra/02-setup-monitoring.sh` |
| Lambda's `ServerSideEncryption="aws:kms"` on every PutObject | `lambdas/shared/storage.py` |
| Layer staging (the same SSE constraint applies to layer zip uploads) | `infra/lambda/build-layer.sh` |

## 8. What's NOT in this doc

- **Column-level masking** (e.g. `XXX-XX-1234` for SSN) — that's
  warehouse-side, see [`pii-handling.md`](pii-handling.md) §3.
- **The unmasking-request workflow** (Jira approval, time-bound tag) — see
  [`pii-handling.md`](pii-handling.md) §4.
- **Tokenization vault** — Phase 4 may add a separate
  Vault/Tokenex-style indirection for the highest-sensitivity fields. Not
  in Phase 1.
- **Data-subject rights (GDPR/CCPA)** — the right-to-be-forgotten
  mechanism uses tombstones in dbt, not bucket-level deletes. See
  [`pii-handling.md`](pii-handling.md) §6.
