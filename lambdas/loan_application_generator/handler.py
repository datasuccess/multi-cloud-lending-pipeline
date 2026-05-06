"""Lambda entry point for the loan_applications generator.

Order of operations is the contract:
  1. generate rows
  2. arrow table
  3. write parquet
  4. read parquet back
  5. validate shape
  6. write manifest (with validation result)
  7. write runs-ledger entry (always — success or failure)
  8. if errors: raise (no _SUCCESS, Lambda fails, P1 alarm fires)
  9. write _SUCCESS (atomic readiness signal for downstream)
 10. emit EMF metrics

Event payload (all optional):
  - rows: int (default ROWS_PER_RUN env var → 12000)
  - seed: int (default None → random)
  - ingest_date: "YYYY-MM-DD" (default today UTC)

The handler is split into a pure `run()` that returns a result dict and a
thin `lambda_handler` that decorates it with Powertools + reads env.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from lambdas.loan_application_generator.generator import (
    GENERATOR_VERSION,
    SOURCE,
    make_rows,
)
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
from lambdas.shared.parquet_writer import (
    LOAN_APPLICATIONS_SCHEMA,
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
# Env-driven so test-mode deploys can lower this to let UNDERSHOOT (100-450
# rows) pass validation and surface as a low-volume alarm instead of an
# errors alarm. Prod keeps the strict 10k floor.
MIN_ROWS = int(os.environ.get("MIN_ROWS", 10_000))

logger = get_logger("loan-app-generator")
metrics = get_metrics("loan-app-generator")


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


def _parse_ingest_date(raw: str | None) -> date:
    if raw:
        return date.fromisoformat(raw)
    return datetime.now(timezone.utc).date()


def _partition_uri(base: str, ingest_date: date) -> str:
    return join(
        base,
        "raw",
        SOURCE,
        f"ingest_date={ingest_date.isoformat()}",
    )


def _object_stem(ingest_at: datetime, run_id: str) -> str:
    return f"{ingest_at.strftime('%Y-%m-%dT%H-%M-%SZ')}_{run_id[:8]}"


def run(
    *,
    base_uri: str,
    rows_n: int,
    seed: int | None,
    ingest_date: date,
    trigger: str,
    lambda_request_id: str | None = None,
    anomaly: Anomaly | None = None,
) -> RunResult:
    run_id = uuid4().hex
    started_at = datetime.now(timezone.utc)

    if anomaly is None:
        anomaly = pick_anomaly()
    if anomaly is not Anomaly.NONE:
        logger.append_keys(anomaly=anomaly.value)

    if anomaly is Anomaly.SKIP:
        # No S3 writes at all → no _SUCCESS, no manifest, no metrics. The
        # freshness alarm trips after the missing-heartbeat window. Lambda
        # still exits cleanly so the errors alarm is unaffected.
        raise RunSkipped(run_id=run_id)

    if anomaly is Anomaly.UNDERSHOOT:
        rows_n = undershoot_rows()

    rows = make_rows(rows_n, ingest_date, seed=seed, ingest_at=started_at)
    table = rows_to_table(rows, LOAN_APPLICATIONS_SCHEMA)

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
        schema=LOAN_APPLICATIONS_SCHEMA,
        min_rows=MIN_ROWS,
    )
    if anomaly is Anomaly.SILENT_FAIL:
        # Force the no-_SUCCESS path even though parquet+manifest landed —
        # exercises the errors alarm without corrupting validation logic.
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
        schema=LOAN_APPLICATIONS_SCHEMA,
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
    )

    if not validation_passed:
        raise ValidationFailed(result)

    write_success(success_uri)

    channel_counts = Counter(r["channel"] for r in rows)
    record_run_metrics(
        metrics,
        source=SOURCE,
        rows=result.rows,
        bytes_=result.bytes,
        duration_ms=result.duration_ms,
        channel_counts=dict(channel_counts),
    )

    return result


class ValidationFailed(Exception):
    def __init__(self, result: RunResult) -> None:
        self.result = result
        super().__init__(
            f"validation failed for run_id={result.run_id}: {result.validation_errors}"
        )


class RunSkipped(Exception):
    """Anomaly engine picked SKIP — no artefacts written, freshness alarm trips."""

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

    logger.append_keys(
        source=SOURCE,
        ingest_date=ingest_date.isoformat(),
        generator_version=GENERATOR_VERSION,
    )
    logger.info("loan_app_generator.start", extra={"rows": rows_n, "trigger": trigger})

    try:
        result = run(
            base_uri=base_uri,
            rows_n=rows_n,
            seed=seed,
            ingest_date=ingest_date,
            trigger=trigger,
            lambda_request_id=lambda_request_id,
        )
    except RunSkipped as exc:
        logger.warning("loan_app_generator.skipped", extra={"run_id": exc.run_id})
        return {"status": "skipped", "run_id": exc.run_id}

    logger.info(
        "loan_app_generator.success",
        extra={
            "run_id": result.run_id,
            "rows": result.rows,
            "bytes": result.bytes,
            "duration_ms": result.duration_ms,
            "parquet_uri": result.parquet_uri,
        },
    )

    return {
        "status": "success",
        "run_id": result.run_id,
        "parquet_uri": result.parquet_uri,
        "manifest_uri": result.manifest_uri,
        "success_uri": result.success_uri,
        "rows": result.rows,
        "bytes": result.bytes,
        "duration_ms": result.duration_ms,
    }
