from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lambdas.shared.runs_ledger import (
    build_run_record,
    runs_ledger_uri,
    write_run_entry,
)


def test_runs_ledger_uri_layout():
    uri = runs_ledger_uri(
        "/tmp/lake", "loan_applications", "2026-05-04", "abc123"
    )
    assert uri == (
        "/tmp/lake/_pipeline_runs/source=loan_applications/"
        "year=2026/month=05/day=04/run-abc123.jsonl"
    )


def test_build_record_and_write(tmp_path):
    started = datetime(2026, 5, 4, 3, 0, 0, tzinfo=timezone.utc)
    finished = started + timedelta(milliseconds=4753)
    record = build_run_record(
        run_id="abc",
        source="loan_applications",
        started_at=started,
        finished_at=finished,
        status="success",
        rows=12000,
        bytes_=3145728,
        trigger="eventbridge:lending-loan-app-daily",
        lambda_request_id="req-1",
    )
    assert record["duration_ms"] == 4753
    assert record["status"] == "success"

    target = str(tmp_path / "ledger.jsonl")
    write_run_entry(target, record)
    write_run_entry(target, {**record, "status": "success", "run_id": "def"})
    lines = Path(target).read_text().strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["run_id"] == "abc"
    assert parsed[1]["run_id"] == "def"
