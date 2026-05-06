"""Lambda entry point for the `customers` generator.

Customers is the **root** of the Phase 2 dependency graph — no parents
upstream. Within itself, returning customers are sampled from prior
`customers` partitions so SCD2 in Phase 5 has change rows to track.

Order of operations is the same contract as Phase 1's loan-app generator:
  1. discover prior partition (best-effort — first run has none)
  2. read parent rows (only for the returning share)
  3. generate rows (split: 95% net-new + 5% returning)
  4. arrow table → parquet → read back
  5. validate shape
  6. manifest + ledger
  7. on errors: raise (no _SUCCESS)
  8. _SUCCESS + EMF metrics

Event payload (all optional):
  - rows: int                 default ROWS_PER_RUN env → 12000
  - seed: int                 default None → random
  - ingest_date: "YYYY-MM-DD" default today UTC
  - returning_share: float    default RETURNING_SHARE env → 0.05
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from lambdas.customer_generator.generator import (
    GENERATOR_VERSION,
    RETURNING_SHARE_DEFAULT,
    SOURCE,
    make_rows,
)
from lambdas.customer_generator.schema import CUSTOMERS_SCHEMA
from lambdas.shared.anomaly import (
    Anomaly,
    SLOW_SLEEP_SECONDS,
    pick_anomaly,
    undershoot_rows,
)
from lambdas.shared.manifest import build_manifest, write_manifest, write_success
from lambdas.shared.observability import (
    get_logger,
    get_metrics,
    record_run_metrics,
)
from lambdas.shared.parent_partition import (
    latest_success_partition,
    read_parent_columns,
)
from lambdas.shared.parquet_writer import (
    read_parquet,
    rows_to_table,
    write_parquet,
)
from lambdas.shared.runs_ledger import (
    build_run_record,
    runs_ledger_uri,
    write_run_entry,
)
from lambdas.shared.storage import join
from lambdas.shared.validation import validate_table

DEFAULT_ROWS = 12_000
MIN_ROWS = int(os.environ.get("MIN_ROWS", 8_000))
RETURNING_SHARE = float(os.environ.get("RETURNING_SHARE", RETURNING_SHARE_DEFAULT))

# Columns we need from the parent partition to construct a returning row.
PARENT_COLUMNS = [
    "customer_id",
    "first_name",
    "last_name",
    "email",
    "phone",
    "date_of_birth",
    "address_line1",
    "city",
    "state",
    "zip",
    "employment_status",
    "annual_income",
    "created_at",
]

logger = get_logger("customers-generator")
metrics = get_metrics("customers-generator")


@dataclass
class RunResult:
    run_id: str
    parquet_uri: str
    manifest_uri: str
    success_uri: str
    rows: int
    bytes: int
    duration_ms: int
    validation_passed: bool
    validation_errors: list[str]
    parent_partition_uri: str | None
    returning_count: int


def _parse_ingest_date(raw: str | None) -> date:
    if raw:
        return date.fromisoformat(raw)
    return datetime.now(timezone.utc).date()


def _partition_uri(base: str, ingest_date: date) -> str:
    return join(base, "raw", SOURCE, f"ingest_date={ingest_date.isoformat()}")


def _object_stem(ingest_at: datetime, run_id: str) -> str:
    return f"{ingest_at.strftime('%Y-%m-%dT%H-%M-%SZ')}_{run_id[:8]}"


def _load_parent_rows(
    base_uri: str, ingest_date: date
) -> tuple[list[dict], str | None]:
    """Find prior `customers` partition (excluding today's, if any) and load
    the columns we need to build returning rows. Returns ([], None) on the
    very first run, when no prior partition exists yet."""
    parent_uri = latest_success_partition(base_uri, SOURCE, before=ingest_date)
    if parent_uri is None:
        return [], None
    cols = read_parent_columns(parent_uri, PARENT_COLUMNS)
    n = len(cols["customer_id"])
    rows = [{c: cols[c][i] for c in PARENT_COLUMNS} for i in range(n)]
    return rows, parent_uri


def run(
    *,
    base_uri: str,
    rows_n: int,
    seed: int | None,
    ingest_date: date,
    trigger: str,
    lambda_request_id: str | None = None,
    anomaly: Anomaly | None = None,
    returning_share: float = RETURNING_SHARE,
) -> RunResult:
    run_id = uuid4().hex
    started_at = datetime.now(timezone.utc)

    if anomaly is None:
        anomaly = pick_anomaly()
    if anomaly is not Anomaly.NONE:
        logger.append_keys(anomaly=anomaly.value)

    if anomaly is Anomaly.SKIP:
        raise RunSkipped(run_id=run_id)

    if anomaly is Anomaly.UNDERSHOOT:
        rows_n = undershoot_rows()

    parent_rows, parent_partition_uri = _load_parent_rows(base_uri, ingest_date)
    if parent_partition_uri is None:
        logger.warning(
            "customers.no_parent_partition",
            extra={"note": "first-ever run; 100% net-new customers"},
        )
    else:
        logger.append_keys(parent_partition=parent_partition_uri)

    rows = make_rows(
        rows_n,
        ingest_date,
        seed=seed,
        ingest_at=started_at,
        parent_rows=parent_rows or None,
        returning_share=returning_share,
    )
    returning_count = sum(1 for r in rows if r["is_returning"])
    table = rows_to_table(rows, CUSTOMERS_SCHEMA)

    partition_uri = _partition_uri(base_uri, ingest_date)
    stem = _object_stem(started_at, run_id)
    parquet_uri = join(partition_uri, f"{stem}.parquet")
    manifest_uri = join(partition_uri, f"{stem}.parquet.manifest.json")
    success_uri = join(partition_uri, "_SUCCESS")

    bytes_written = write_parquet(parquet_uri, table)

    if anomaly is Anomaly.SLOW:
        time.sleep(SLOW_SLEEP_SECONDS)

    table_back = read_parquet(parquet_uri)
    errors = validate_table(
        table_back,
        expected_rows=rows_n,
        schema=CUSTOMERS_SCHEMA,
        min_rows=MIN_ROWS,
    )
    if anomaly is Anomaly.SILENT_FAIL:
        errors = list(errors) + ["anomaly:silent_fail (chaos)"]
    validation_passed = not errors

    finished_at = datetime.now(timezone.utc)

    manifest = build_manifest(
        run_id=run_id,
        source=SOURCE,
        generator_version=GENERATOR_VERSION,
        ingest_date=ingest_date.isoformat(),
        parquet_key=parquet_uri,
        rows=table_back.num_rows,
        bytes_=bytes_written,
        schema=CUSTOMERS_SCHEMA,
        started_at=started_at,
        finished_at=finished_at,
        validation_passed=validation_passed,
        validation_errors=errors,
    )
    write_manifest(manifest_uri, manifest)

    ledger_uri = runs_ledger_uri(base_uri, SOURCE, ingest_date.isoformat(), run_id)
    write_run_entry(
        ledger_uri,
        build_run_record(
            run_id=run_id,
            source=SOURCE,
            started_at=started_at,
            finished_at=finished_at,
            status="success" if validation_passed else "failure",
            rows=table_back.num_rows,
            bytes_=bytes_written,
            trigger=trigger,
            lambda_request_id=lambda_request_id,
            error="; ".join(errors) if errors else None,
        ),
    )

    duration_ms = manifest["duration_ms"]
    result = RunResult(
        run_id=run_id,
        parquet_uri=parquet_uri,
        manifest_uri=manifest_uri,
        success_uri=success_uri,
        rows=table_back.num_rows,
        bytes=bytes_written,
        duration_ms=duration_ms,
        validation_passed=validation_passed,
        validation_errors=errors,
        parent_partition_uri=parent_partition_uri,
        returning_count=returning_count,
    )

    if not validation_passed:
        raise ValidationFailed(result)

    write_success(success_uri)

    kyc_counts = Counter(r["kyc_status"] for r in rows)
    record_run_metrics(
        metrics,
        source=SOURCE,
        rows=result.rows,
        bytes_=result.bytes,
        duration_ms=result.duration_ms,
        channel_counts=dict(kyc_counts),
    )

    return result


class ValidationFailed(Exception):
    def __init__(self, result: RunResult) -> None:
        self.result = result
        super().__init__(
            f"validation failed for run_id={result.run_id}: {result.validation_errors}"
        )


class RunSkipped(Exception):
    """Anomaly engine picked SKIP — no artefacts written."""

    def __init__(self, *, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run skipped by anomaly injection: run_id={run_id}")


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    base_uri = os.environ.get("RAW_BUCKET_URI") or f"s3://{os.environ['RAW_BUCKET']}"
    rows_n = int(event.get("rows", os.environ.get("ROWS_PER_RUN", DEFAULT_ROWS)))
    seed = event.get("seed")
    ingest_date = _parse_ingest_date(event.get("ingest_date"))
    trigger = event.get("trigger", "manual")
    lambda_request_id = getattr(context, "aws_request_id", None) if context else None
    returning_share = float(event.get("returning_share", RETURNING_SHARE))

    logger.append_keys(
        source=SOURCE,
        ingest_date=ingest_date.isoformat(),
        generator_version=GENERATOR_VERSION,
    )
    logger.info("customers_generator.start", extra={"rows": rows_n, "trigger": trigger})

    try:
        result = run(
            base_uri=base_uri,
            rows_n=rows_n,
            seed=seed,
            ingest_date=ingest_date,
            trigger=trigger,
            lambda_request_id=lambda_request_id,
            returning_share=returning_share,
        )
    except RunSkipped as exc:
        logger.warning("customers_generator.skipped", extra={"run_id": exc.run_id})
        return {"status": "skipped", "run_id": exc.run_id}

    logger.info(
        "customers_generator.success",
        extra={
            "run_id": result.run_id,
            "rows": result.rows,
            "returning": result.returning_count,
            "bytes": result.bytes,
            "duration_ms": result.duration_ms,
            "parquet_uri": result.parquet_uri,
            "parent_partition_uri": result.parent_partition_uri,
        },
    )

    return {
        "status": "success",
        "run_id": result.run_id,
        "parquet_uri": result.parquet_uri,
        "manifest_uri": result.manifest_uri,
        "success_uri": result.success_uri,
        "rows": result.rows,
        "returning": result.returning_count,
        "bytes": result.bytes,
        "duration_ms": result.duration_ms,
        "parent_partition_uri": result.parent_partition_uri,
    }
