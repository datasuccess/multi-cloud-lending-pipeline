# PII handling — strategy across all phases

This is a cross-phase document. Every phase that touches storage, IAM, dbt, or warehouse access points back here.

> **Operating principle.** Collect PII because the business needs it (KYC, fraud, support). **Encrypt at rest. Mask by default. Unmask only via audited, time-bounded, ticket-driven access. Log every read.**

> **Companion doc:** [`kms-and-pii-access.md`](kms-and-pii-access.md) covers
> the **AWS layer** (KMS envelope encryption, the encrypt-only IAM pattern,
> the three-role model, CloudTrail audit). This doc covers the **warehouse
> layer** (column-level masking, unmasking workflow, GDPR rights). A user
> only sees raw PII when **both** layers grant it.

---

## 1. Classification (the taxonomy)

| Tier                    | Examples                                                          | Default access            |
|-------------------------|-------------------------------------------------------------------|---------------------------|
| **DI — direct identifier** | name, SSN, email, phone, street_address, gov_id_number, IP        | Masked                    |
| **QI — quasi-identifier**  | DOB, zip, state, user_agent (device fingerprint risk)             | Visible at low precision (e.g. zip3 not zip5, year-of-birth not full DOB) |
| **Financial — sensitive**  | annual_income, existing_debt, account_number (later phases)       | Visible to analytics roles, never exported |
| **Pseudonymous**           | application_id, customer_id (UUIDs)                               | Always visible            |
| **Free-text**              | declared_purpose_text — may contain accidental PII                | Masked + Macie scan       |

The taxonomy is encoded in `dbt/macros/pii/pii_classes.sql` (Phase 4). Each model declares which columns belong to which tier; masking macros consume the tag.

## 2. Storage — what's where

```
                                        ┌─────────────────────────┐
                                        │  KMS key                │
                                        │  alias/lending-pii      │
                                        │  rotation: yearly       │
                                        └────────────┬────────────┘
                                                     │ envelope encryption
                ┌──────────────────────┐    ┌────────▼────────────┐
   Lambda role  │  S3 raw bucket       │    │  Snowflake / RS     │
  (encrypt only)│  SSE-KMS, bucket-key │    │  external Iceberg   │
                │  TLS-only policy     │◄───┤  reads via STORAGE  │
                └──────────────────────┘    │  INTEGRATION using  │
                          │                 │  pii-loader-role    │
                          │                 └─────────────────────┘
                          │ read+decrypt
                ┌─────────▼─────────────────────────────────────┐
                │  Two human/service IAM roles                  │
                │  ─────────────────────────────────────────────│
                │  pii-loader-role (Snowflake/RS service acct)  │
                │     can decrypt; loads warehouse              │
                │                                               │
                │  pii-investigator-role (audited human)        │
                │     can decrypt; assume via MFA + 1h STS;     │
                │     CloudTrail logged on every assume         │
                └───────────────────────────────────────────────┘
```

Phase by phase:

| Phase | What we add                                                                           |
|-------|---------------------------------------------------------------------------------------|
| 1     | KMS key + alias, S3 SSE-KMS, both IAM roles (loader + investigator) defined but unused |
| 3     | Iceberg metadata also encrypted via the same KMS key                                  |
| 4     | Snowflake `STORAGE INTEGRATION` mapping `pii-loader-role`; Snowflake masking policies |
| 5     | dbt macro `mask_pii('email')` applied in staging models for default consumers          |
| 6     | Redshift IAM-role auth + Redshift dynamic data masking on the same columns            |
| 8     | Airflow DAG that *never* surfaces PII in logs (structured logging redactions)         |
| 11    | Streaming variant: Kinesis stream encrypted with same KMS key                         |
| 13    | Cost retro includes KMS request volume, audit-log volume                              |

## 3. Masking — implementation per warehouse

### 3.1 Snowflake (Phase 4)

```sql
CREATE MASKING POLICY pii_email_mask AS (val string) RETURNS string ->
  CASE
    WHEN CURRENT_ROLE() IN ('PII_READER', 'PII_ADMIN') THEN val
    ELSE REGEXP_REPLACE(val, '(^[^@]{1})[^@]*(@.*$)', '\\1***\\2')
  END;

ALTER TABLE staging.stg_loan_applications
  MODIFY COLUMN email SET MASKING POLICY pii_email_mask;
```

A separate policy per data type (email partial-mask, SSN last-4-only, name initials, full redaction for free-text). Catalogued in `dbt/macros/pii/snowflake_policies.sql`.

### 3.2 Redshift (Phase 6)

Redshift Dynamic Data Masking (DDM):

```sql
CREATE MASKING POLICY pii_email_mask
  WITH (val varchar)
  USING (
    CASE WHEN current_user_id() IN (SELECT user_id FROM pii_readers) THEN val
         ELSE regexp_replace(val, '(^[^@]{1})[^@]*(@.*$)', '\\1***\\2') END
  );

ATTACH MASKING POLICY pii_email_mask
  ON staging.stg_loan_applications(email)
  TO ROLE analyst;
```

### 3.3 BigQuery (Phase 10)

Policy tags + column-level access:

```sql
ALTER TABLE staging.stg_loan_applications
  ALTER COLUMN email SET OPTIONS (
    description = 'Email — direct identifier',
    policy_tags = ['projects/<p>/locations/us/taxonomies/<t>/policyTags/<email_tag>']
  );
```

The `email_tag` grants are managed via IAM in `infra/terraform/99-gcp/`.

## 4. The unmasking workflow (the "request to see real data" flow)

The most important real-world workflow. We model this even though it's only minimally automated in this project — the *roles, policies, and audit trail* are what we learn.

### 4.1 The flow (process)

```
  1.  Investigator                                  2.  Manager
      Files ticket: "Need real customer name           Reviews ticket
      on app_id=… for fraud case #FR-1234"             Approves / rejects in 1 business day
                                                       Approval recorded as ticket comment
                          │
                          ▼
                3.  IAM admin (or self-service script)
                    Adds investigator email to the
                    `pii-investigator-role` trust policy condition
                    with a 4-hour expiration tag
                          │
                          ▼
                4.  Investigator
                    `aws sts assume-role --role-arn pii-investigator-role
                       --role-session-name FR-1234`
                    (MFA prompt; session = 1h, max-renewals 4)
                          │
                          ▼
                5.  Investigator queries warehouse
                    SET ROLE PII_READER;
                    SELECT first_name, last_name, ssn
                      FROM staging.stg_loan_applications
                     WHERE application_id = '…';
                          │
                          ▼
                6.  Audit trail
                    CloudTrail logs every AssumeRole + KMS Decrypt
                    Snowflake QUERY_HISTORY logs every PII_READER query
                    Both feed daily report to compliance@
                          │
                          ▼
                7.  Auto-revoke (4h after step 3)
                    EventBridge schedule strips investigator
                    from the role's trust condition
                          │
                          ▼
                8.  Quarterly review
                    Compliance reviews all PII access events,
                    flags anomalies (off-hours, unusual volume,
                    queries without ticket reference)
```

### 4.2 What we build (this project)

| Component                                | Phase | Form                                                                    |
|------------------------------------------|-------|-------------------------------------------------------------------------|
| `pii-investigator-role` IAM role + trust | 1     | Manual CLI (Phase 2 → Terraform)                                        |
| MFA-required STS assume                  | 1     | Trust policy condition `aws:MultiFactorAuthPresent=true`                |
| 4-hour session limit                     | 1     | `MaxSessionDuration=14400` on the role                                  |
| `PII_READER` Snowflake role              | 4     | dbt-managed grant; default scope = empty list of users                  |
| `pii_*_mask` policies                    | 4     | dbt macros generate them                                                |
| Auto-revoke schedule                     | 8     | Airflow DAG; reads ticket system, syncs trust-policy condition          |
| Compliance daily report                  | 8     | Airflow DAG → S3 → email (CloudTrail + Snowflake `QUERY_HISTORY`)       |
| Streamlit "PII access audit" page        | 9     | Read-only view of who accessed what when                                 |
| Quarterly review checklist               | 13    | Markdown runbook                                                        |

### 4.3 What we deliberately do NOT build

- Real ticket-system integration (Jira/ServiceNow). Mocked via a markdown ticket-template + manual approval comment.
- Self-serve UI for the investigator. CLI-only.
- Automated PII-detection in `declared_purpose_text` (Macie/Comprehend). Stretch goal in Phase 13.
- Tokenisation / format-preserving encryption. Masking is enough for analytics; tokenisation is for case management systems out of scope here.

## 5. Audit logging — what we log, where it lives

| Event                            | Source                                | Retention | Who reads                    |
|----------------------------------|---------------------------------------|-----------|------------------------------|
| `AssumeRole` on investigator role| CloudTrail management events          | 1 year    | compliance@, security@       |
| KMS `Decrypt` calls              | CloudTrail data events                | 1 year    | compliance@                  |
| S3 `GetObject` on raw            | CloudTrail data events                | 1 year    | compliance@                  |
| Snowflake queries by PII_READER  | `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` (45-day retention) → daily snapshot to S3 | 1 year | compliance@, data-eng@ |
| Redshift queries by analyst role | `STL_QUERY` + `SVL_QLOG` → snapshot   | 1 year    | compliance@                  |
| Lambda invocations (no PII)      | CloudWatch logs                       | 7 days    | data-eng@                    |

Two rules from real ops:

1. **Audit logs never expire silently.** A 30-day rolling log is useless for incidents discovered six months later. We snapshot Snowflake/Redshift query history nightly to S3 with object-lock so deletion requires the compliance role.
2. **Audit logs themselves are PII.** Email + role + timestamp + query text reveals investigation context. Same encryption, same role-gating as the underlying data.

## 6. Data-subject rights (GDPR / CCPA-style requests)

Even synthetic data, we wire the workflow so we know how to do it for real:

| Right                       | Implementation pattern                                                            |
|-----------------------------|------------------------------------------------------------------------------------|
| **Access** ("show me")       | Investigator workflow above, scoped to the requestor's `customer_id`.              |
| **Rectification**           | New row in `loan_applications` with same `application_id`, later `_ingest_at`. DV2 satellite tracks history.|
| **Erasure** ("forget me")   | Hard problem. Pattern: delete from raw S3 (DELETE-by-prefix is impossible in append-only Parquet — use Iceberg's row-level delete from Phase 3). Tombstone in DV2 hub-sat. Audit-log the deletion. |
| **Portability**             | Export the customer's rows in JSON via a one-shot dbt model.                       |
| **Objection / restriction** | Per-customer flag in `customers` table; dbt staging excludes flagged customers from non-essential models. |

Erasure deserves its own doc once we've done it once — added in Phase 3 when Iceberg row-deletes are available.

## 7. Things we do NOT collect (deliberately)

Even if Faker can produce them — to keep the blast radius small:

- **Plain credit card numbers.** Never. Use Faker masked tokens (`tok_xxxx`) in payments source.
- **Photographs / biometrics.** Out of scope.
- **Health information.** Loan purpose may be "medical" but we don't store diagnoses.
- **Children's data.** DOB ≥ 18 years ago enforced in generator.

## 8. Operational reminders

- **Review this doc when starting any phase that touches IAM, S3, the warehouse, or dbt.** Update the phase row in §2.
- **No PII in commits.** `.gitignore` already blocks `*.csv`, `*.json` keys, `secrets.toml`. Add to it any sample-data file that wasn't generated in-tree.
- **No PII in CI logs.** Powertools redacts known PII fields by default; configure the redaction list in `lambdas/shared/logging.py`.
- **No PII in Slack/email.** Never paste a query result containing real-looking names/SSNs in a chat. Use ticket-system attachments with access controls.
