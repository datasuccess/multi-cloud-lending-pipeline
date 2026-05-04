"""End-to-end test of the Lambda handler against a tmp_path lake.

Asserts the artefact landing order and that a deliberate validation failure
prevents `_SUCCESS` from being written. No AWS in this test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lambdas.loan_application_generator import handler as h
from lambdas.shared.parquet_writer import read_parquet


def _read_partition(base: Path) -> Path:
    parts = list((base / "raw" / "loan_applications").iterdir())
    assert len(parts) == 1
    return parts[0]


def test_handler_happy_path(tmp_path):
    base = tmp_path / "lake"
    result = h.run(
        base_uri=str(base),
        rows_n=10_000,
        seed=42,
        ingest_date=h.date.fromisoformat("2026-05-04"),
        trigger="test",
    )
    assert result.validation_passed is True
    assert result.rows == 10_000

    partition = _read_partition(base)
    files = sorted(p.name for p in partition.iterdir())
    assert any(f.endswith(".parquet") for f in files)
    assert any(f.endswith(".manifest.json") for f in files)
    assert "_SUCCESS" in files

    manifest_path = next(partition.glob("*.manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["validation_passed"] is True
    assert manifest["rows"] == 10_000
    assert manifest["validation_errors"] == []
    assert manifest["schema_hash"].startswith("sha256:")

    table = read_parquet(result.parquet_uri)
    assert table.num_rows == 10_000

    ledger = list((base / "_pipeline_runs").rglob("run-*.jsonl"))
    assert len(ledger) == 1
    record = json.loads(ledger[0].read_text().strip())
    assert record["status"] == "success"
    assert record["rows"] == 10_000


def test_handler_validation_failure_blocks_success(tmp_path, monkeypatch):
    """If post-write validation fails, _SUCCESS is NOT written and we raise."""
    base = tmp_path / "lake"

    monkeypatch.setattr(h, "MIN_ROWS", 50_000)

    with pytest.raises(h.ValidationFailed) as exc_info:
        h.run(
            base_uri=str(base),
            rows_n=5,
            seed=42,
            ingest_date=h.date.fromisoformat("2026-05-04"),
            trigger="test-chaos",
        )

    result = exc_info.value.result
    assert result.validation_passed is False
    assert any("too few rows" in e for e in result.validation_errors)

    partition = _read_partition(base)
    files = {p.name for p in partition.iterdir()}
    # parquet + manifest exist; _SUCCESS does NOT.
    assert any(f.endswith(".parquet") for f in files)
    assert any(f.endswith(".manifest.json") for f in files)
    assert "_SUCCESS" not in files

    manifest_path = next(partition.glob("*.manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["validation_passed"] is False
    assert len(manifest["validation_errors"]) >= 1

    ledger_files = list((base / "_pipeline_runs").rglob("run-*.jsonl"))
    assert len(ledger_files) == 1
    record = json.loads(ledger_files[0].read_text().strip())
    assert record["status"] == "failure"
    assert "too few rows" in record["error"]
