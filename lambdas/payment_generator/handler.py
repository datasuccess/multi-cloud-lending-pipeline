"""Lambda entry point for the `payments` generator.

Two parents:
- `loan_drawdowns` (today's): FK source for the period's payment events.
- `payments` (yesterday's): prior state for the Markov transition matrix.

Event payload (all optional):
  - seed: int        default None → random
  - ingest_date: "YYYY-MM-DD" default today UTC
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from lambdas.payment_generator.generator import (
    GENERATOR_VERSION,
    PAYMENTS_CAP,
    SOURCE,
    make_rows,
)
from lambdas.payment_generator.schema import PAYMENTS_SCHEMA
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

MIN_ROWS = int(os.environ.get("MIN_ROWS", 20_000))
DRAWDOWNS_SOURCE = "loan_drawdowns"

DRAWDOWN_COLUMNS = [
    "drawdown_id",
    "customer_id",
    "drawn_amount",
    "apr_pct",
    "term_months",
]
PRIOR_PAYMENT_COLUMNS = ["drawdown_id", "payment_status"]

logger = get_logger("payments-generator")
metrics = get_metrics("payments-generator")


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
    drawdowns_partition_uri: str
    prior_payments_partition_uri: str | None
    paid_full_count: int
    paid_partial_count: int
    missed_count: int
    waived_count: int


def _parse_ingest_date(raw: str | None) -> date:
    if raw:
        return date.fromisoformat(raw)
    return datetime.now(timezone.utc).date()


def _partition_uri(base: str, ingest_date: date) -> str:
    return join(base, "raw", SOURCE, f"ingest_date={ingest_date.isoformat()}")


def _object_stem(ingest_at: datetime, run_id: str) -> str:
    return f"{ingest_at.strftime('%Y-%m-%dT%H-%M-%SZ')}_{run_id[:8]}"


def _load_drawdowns(base_uri: str) -> tuple[list[dict], str]:
    parent_uri = latest_success_partition(base_uri, DRAWDOWNS_SOURCE)
    if parent_uri is None:
        raise ParentNotFound(f"No `_SUCCESS`'d {DRAWDOWNS_SOURCE} partition")
    cols = read_parent_columns(parent_uri, DRAWDOWN_COLUMNS)
    n = len(cols["drawdown_id"])
    rows = [{c: cols[c][i] for c in DRAWDOWN_COLUMNS} for i in range(n)]
    return rows, parent_uri


def _load_prior_payment_states(
    base_uri: str, *, ingest_date: date
) -> tuple[dict[str, str], str | None]:
    """Latest payments partition strictly before today's ingest_date.

    Returns ({}, None) on first-ever run. The Markov matrix treats
    missing prior state as "paid_full / new" (the favorable starting
    state).
    """
    prior_uri = latest_success_partition(base_uri, SOURCE, before=ingest_date)
    if prior_uri is None:
        return {}, None
    cols = read_parent_columns(prior_uri, PRIOR_PAYMENT_COLUMNS)
    n = len(cols["drawdown_id"])
    return {cols["drawdown_id"][i]: cols["payment_status"][i] for i in range(n)}, prior_uri


def run(
    *,
    base_uri: str,
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
        raise RunSkipped(run_id=run_id)

    drawdowns, drawdowns_uri = _load_drawdowns(base_uri)
    logger.append_keys(drawdowns_partition=drawdowns_uri)

    prior_states, prior_uri = _load_prior_payment_states(
        base_uri, ingest_date=ingest_date
    )
    if prior_uri is not None:
        logger.append_keys(prior_payments_partition=prior_uri)
    else:
        logger.warning(
            "payments.no_prior_partition",
            extra={"note": "first-ever run; all drawdowns start from 'paid_full' state"},
        )

    if anomaly is Anomaly.UNDERSHOOT:
        keep = min(undershoot_rows(), len(drawdowns))
        drawdowns = drawdowns[:keep]

    expected_rows = min(len(drawdowns), PAYMENTS_CAP)
    rows = make_rows(
        drawdowns,
        seed=seed,
        ingest_at=started_at,
        scheduled_at=ingest_date,
        prior_status_by_drawdown=prior_states,
    )
    table = rows_to_table(rows, PAYMENTS_SCHEMA)

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
        expected_rows=expected_rows,
        schema=PAYMENTS_SCHEMA,
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
        schema=PAYMENTS_SCHEMA,
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

    counts = Counter(r["payment_status"] for r in rows)
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
        drawdowns_partition_uri=drawdowns_uri,
        prior_payments_partition_uri=prior_uri,
        paid_full_count=counts.get("paid_full", 0),
        paid_partial_count=counts.get("paid_partial", 0),
        missed_count=counts.get("missed", 0),
        waived_count=counts.get("waived", 0),
    )

    if not validation_passed:
        raise ValidationFailed(result)

    write_success(success_uri)

    record_run_metrics(
        metrics,
        source=SOURCE,
        rows=result.rows,
        bytes_=result.bytes,
        duration_ms=result.duration_ms,
        channel_counts=dict(counts),
    )

    return result


class ValidationFailed(Exception):
    def __init__(self, result: RunResult) -> None:
        self.result = result
        super().__init__(
            f"validation failed for run_id={result.run_id}: {result.validation_errors}"
        )


class RunSkipped(Exception):
    def __init__(self, *, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run skipped by anomaly injection: run_id={run_id}")


class ParentNotFound(Exception):
    """No upstream `_SUCCESS`'d drawdowns partition."""


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    base_uri = os.environ.get("RAW_BUCKET_URI") or f"s3://{os.environ['RAW_BUCKET']}"
    seed = event.get("seed")
    ingest_date = _parse_ingest_date(event.get("ingest_date"))
    trigger = event.get("trigger", "manual")
    lambda_request_id = getattr(context, "aws_request_id", None) if context else None

    logger.append_keys(
        source=SOURCE,
        ingest_date=ingest_date.isoformat(),
        generator_version=GENERATOR_VERSION,
    )
    logger.info("payments_generator.start", extra={"trigger": trigger})

    try:
        result = run(
            base_uri=base_uri,
            seed=seed,
            ingest_date=ingest_date,
            trigger=trigger,
            lambda_request_id=lambda_request_id,
        )
    except RunSkipped as exc:
        logger.warning("payments_generator.skipped", extra={"run_id": exc.run_id})
        return {"status": "skipped", "run_id": exc.run_id}

    logger.info(
        "payments_generator.success",
        extra={
            "run_id": result.run_id,
            "rows": result.rows,
            "paid_full": result.paid_full_count,
            "paid_partial": result.paid_partial_count,
            "missed": result.missed_count,
            "waived": result.waived_count,
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
        "paid_full": result.paid_full_count,
        "paid_partial": result.paid_partial_count,
        "missed": result.missed_count,
        "waived": result.waived_count,
        "bytes": result.bytes,
        "duration_ms": result.duration_ms,
    }
