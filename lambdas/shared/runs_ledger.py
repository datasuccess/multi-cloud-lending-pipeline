"""Pipeline runs ledger — JSONL append per invocation.

Single source of truth for "did the pipeline run on day Y?". Survives long
after CloudWatch logs roll off. See docs/monitoring.md §5.2.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from lambdas.shared.storage import append_line, join


def runs_ledger_uri(base_uri: str, source: str, ingest_date: str, run_id: str) -> str:
    """`<base>/_pipeline_runs/source=<src>/year=YYYY/month=MM/day=DD/run-<id>.jsonl`."""
    year, month, day = ingest_date.split("-")
    return join(
        base_uri,
        "_pipeline_runs",
        f"source={source}",
        f"year={year}",
        f"month={month}",
        f"day={day}",
        f"run-{run_id}.jsonl",
    )


def build_run_record(
    *,
    run_id: str,
    source: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    rows: int,
    bytes_: int,
    trigger: str,
    lambda_request_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    record: dict[str, Any] = {
        "run_id": run_id,
        "source": source,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "finished_at": finished_at.isoformat().replace("+00:00", "Z"),
        "status": status,
        "rows": rows,
        "bytes": bytes_,
        "duration_ms": duration_ms,
        "trigger": trigger,
    }
    if lambda_request_id is not None:
        record["lambda_request_id"] = lambda_request_id
    if error is not None:
        record["error"] = error
    return record


def write_run_entry(uri: str, record: dict[str, Any]) -> None:
    append_line(uri, json.dumps(record, sort_keys=True))
