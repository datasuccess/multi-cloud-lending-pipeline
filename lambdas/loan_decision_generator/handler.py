"""Lambda entry point for the `loan_decisions` generator.

Reads two parents — `loan_applications` and `credit_bureau_pulls` —
joins on `application_id`, runs the rule engine, writes parquet +
manifest + `_SUCCESS`.

Event payload (all optional):
  - seed: int        default None → random
  - ingest_date: "YYYY-MM-DD" default today UTC

Row count is derived from the parent — UNDERSHOOT trims the join.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from lambdas.loan_decision_generator.generator import (
    GENERATOR_VERSION,
    SOURCE,
    make_rows,
)
from lambdas.loan_decision_generator.schema import LOAN_DECISIONS_SCHEMA
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

MIN_ROWS = int(os.environ.get("MIN_ROWS", 10_000))

APPLICATIONS_SOURCE = "loan_applications"
BUREAU_SOURCE = "credit_bureau_pulls"

APP_COLUMNS = [
    "application_id",
    "customer_id",
    "amount_requested",
    "annual_income",
    "term_months",
]
BUREAU_COLUMNS = ["application_id", "bureau_score", "pulled_at"]

logger = get_logger("decisions-generator")
metrics = get_metrics("decisions-generator")


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
    apps_partition_uri: str
    bureau_partition_uri: str
    approve_count: int
    decline_count: int
    referred_count: int


def _parse_ingest_date(raw: str | None) -> date:
    if raw:
        return date.fromisoformat(raw)
    return datetime.now(timezone.utc).date()


def _partition_uri(base: str, ingest_date: date) -> str:
    return join(base, "raw", SOURCE, f"ingest_date={ingest_date.isoformat()}")


def _object_stem(ingest_at: datetime, run_id: str) -> str:
    return f"{ingest_at.strftime('%Y-%m-%dT%H-%M-%SZ')}_{run_id[:8]}"


def _join_apps_and_bureau(base_uri: str) -> tuple[list[dict], str, str]:
    apps_uri = latest_success_partition(base_uri, APPLICATIONS_SOURCE)
    bureau_uri = latest_success_partition(base_uri, BUREAU_SOURCE)
    if apps_uri is None:
        raise ParentNotFound(f"No `_SUCCESS`'d {APPLICATIONS_SOURCE} partition")
    if bureau_uri is None:
        raise ParentNotFound(f"No `_SUCCESS`'d {BUREAU_SOURCE} partition")

    apps_cols = read_parent_columns(apps_uri, APP_COLUMNS)
    bureau_cols = read_parent_columns(bureau_uri, BUREAU_COLUMNS)

    bureau_index: dict[str, dict] = {}
    n_bureau = len(bureau_cols["application_id"])
    for i in range(n_bureau):
        app_id = bureau_cols["application_id"][i]
        bureau_index[app_id] = {c: bureau_cols[c][i] for c in BUREAU_COLUMNS}

    joined: list[dict] = []
    n_apps = len(apps_cols["application_id"])
    missing = 0
    for i in range(n_apps):
        app_id = apps_cols["application_id"][i]
        bureau = bureau_index.get(app_id)
        if bureau is None:
            missing += 1
            continue
        joined.append(
            {
                "application_id": app_id,
                "customer_id": apps_cols["customer_id"][i],
                "amount_requested": apps_cols["amount_requested"][i],
                "annual_income": apps_cols["annual_income"][i],
                "term_months": apps_cols["term_months"][i],
                "bureau_score": bureau["bureau_score"],
                "pulled_at": bureau["pulled_at"],
            }
        )
    if missing:
        logger.warning(
            "decisions.bureau_join_misses",
            extra={"missing": missing, "total": n_apps},
        )
    return joined, apps_uri, bureau_uri


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

    joined, apps_uri, bureau_uri = _join_apps_and_bureau(base_uri)
    logger.append_keys(apps_partition=apps_uri, bureau_partition=bureau_uri)

    if anomaly is Anomaly.UNDERSHOOT:
        keep = min(undershoot_rows(), len(joined))
        joined = joined[:keep]

    expected_rows = len(joined)
    rows = make_rows(joined, seed=seed, ingest_at=started_at)
    table = rows_to_table(rows, LOAN_DECISIONS_SCHEMA)

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
        schema=LOAN_DECISIONS_SCHEMA,
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
        schema=LOAN_DECISIONS_SCHEMA,
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

    decision_counts = Counter(r["decision"] for r in rows)
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
        apps_partition_uri=apps_uri,
        bureau_partition_uri=bureau_uri,
        approve_count=decision_counts.get("approved", 0),
        decline_count=decision_counts.get("declined", 0),
        referred_count=decision_counts.get("referred", 0),
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
        channel_counts=dict(decision_counts),
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
    """At least one upstream `_SUCCESS`'d partition is missing."""


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
    logger.info("decisions_generator.start", extra={"trigger": trigger})

    try:
        result = run(
            base_uri=base_uri,
            seed=seed,
            ingest_date=ingest_date,
            trigger=trigger,
            lambda_request_id=lambda_request_id,
        )
    except RunSkipped as exc:
        logger.warning("decisions_generator.skipped", extra={"run_id": exc.run_id})
        return {"status": "skipped", "run_id": exc.run_id}

    logger.info(
        "decisions_generator.success",
        extra={
            "run_id": result.run_id,
            "rows": result.rows,
            "approved": result.approve_count,
            "declined": result.decline_count,
            "referred": result.referred_count,
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
        "approved": result.approve_count,
        "declined": result.decline_count,
        "referred": result.referred_count,
        "bytes": result.bytes,
        "duration_ms": result.duration_ms,
    }
