"""End-to-end test of the Lambda handler against a tmp_path lake.

Asserts the artefact landing order and that a deliberate validation failure
prevents `_SUCCESS` from being written. No AWS in this test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lambdas.loan_application_generator import handler as h
from lambdas.shared.anomaly import Anomaly
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


def test_anomaly_skip_writes_no_artefacts(tmp_path):
    """SKIP raises RunSkipped with no S3 writes — freshness alarm trips."""
    base = tmp_path / "lake"

    with pytest.raises(h.RunSkipped):
        h.run(
            base_uri=str(base),
            rows_n=10_000,
            seed=42,
            ingest_date=h.date.fromisoformat("2026-05-04"),
            trigger="test",
            anomaly=Anomaly.SKIP,
        )

    # Lake is untouched — no partition, no ledger.
    assert not (base / "raw").exists()
    assert not (base / "_pipeline_runs").exists()


def test_anomaly_undershoot_writes_few_rows_and_succeeds(tmp_path, monkeypatch):
    """UNDERSHOOT writes 100-450 rows; with MIN_ROWS=1 validation still passes."""
    base = tmp_path / "lake"
    monkeypatch.setattr(h, "MIN_ROWS", 1)

    result = h.run(
        base_uri=str(base),
        rows_n=10_000,
        seed=42,
        ingest_date=h.date.fromisoformat("2026-05-04"),
        trigger="test",
        anomaly=Anomaly.UNDERSHOOT,
    )

    assert result.validation_passed is True
    assert 100 <= result.rows <= 450  # rows_n was rewritten by undershoot

    partition = _read_partition(base)
    files = {p.name for p in partition.iterdir()}
    assert "_SUCCESS" in files


def test_anomaly_silent_fail_blocks_success(tmp_path):
    """SILENT_FAIL appends a chaos error post-validation → no _SUCCESS."""
    base = tmp_path / "lake"

    with pytest.raises(h.ValidationFailed) as exc_info:
        h.run(
            base_uri=str(base),
            rows_n=10_000,
            seed=42,
            ingest_date=h.date.fromisoformat("2026-05-04"),
            trigger="test",
            anomaly=Anomaly.SILENT_FAIL,
        )

    result = exc_info.value.result
    assert any("silent_fail" in e for e in result.validation_errors)

    partition = _read_partition(base)
    files = {p.name for p in partition.iterdir()}
    assert any(f.endswith(".parquet") for f in files)
    assert "_SUCCESS" not in files


def test_handler_picks_customer_ids_from_parent_partition(tmp_path, monkeypatch):
    """When a customers partition exists, loan_apps reuse customer_ids from it.

    Lands a customers partition first (via the customer_generator handler),
    then runs loan_apps and verifies every customer_id in the loan-app
    partition is present in the customers partition.
    """
    from lambdas.customer_generator import handler as ch
    from lambdas.shared.parent_partition import read_parent_columns

    monkeypatch.setattr(h, "MIN_ROWS", 1)
    monkeypatch.setattr(ch, "MIN_ROWS", 1)
    base = tmp_path / "lake"

    cust_result = ch.run(
        base_uri=str(base),
        rows_n=200,
        seed=1,
        ingest_date=h.date.fromisoformat("2026-05-04"),
        trigger="test",
    )
    assert cust_result.validation_passed is True

    result = h.run(
        base_uri=str(base),
        rows_n=300,
        seed=2,
        ingest_date=h.date.fromisoformat("2026-05-04"),
        trigger="test",
    )
    assert result.validation_passed is True
    assert result.parent_partition_uri is not None
    assert result.parent_partition_uri.endswith("customers/ingest_date=2026-05-04")

    cust_partition = base / "raw" / "customers" / "ingest_date=2026-05-04"
    cust_ids = set(read_parent_columns(str(cust_partition), ["customer_id"])["customer_id"])

    app_table = read_parquet(result.parquet_uri)
    app_customer_ids = set(app_table.column("customer_id").to_pylist())
    assert app_customer_ids.issubset(cust_ids)
    # Sample with replacement (300 apps from 200 customers) → some reuse expected.
    assert len(app_customer_ids) <= 200


def test_handler_falls_back_to_faker_when_no_parent(tmp_path, monkeypatch):
    """No customers partition → all customer_ids are unique uuid4s, run still succeeds."""
    monkeypatch.setattr(h, "MIN_ROWS", 1)
    base = tmp_path / "lake"

    result = h.run(
        base_uri=str(base),
        rows_n=200,
        seed=42,
        ingest_date=h.date.fromisoformat("2026-05-04"),
        trigger="test",
    )
    assert result.validation_passed is True
    assert result.parent_partition_uri is None
    table = read_parquet(result.parquet_uri)
    ids = table.column("customer_id").to_pylist()
    assert len(set(ids)) == len(ids)  # all distinct


def test_anomaly_slow_sleeps_before_validation(tmp_path, monkeypatch):
    """SLOW must call time.sleep(SLOW_SLEEP_SECONDS) — patch sleep so the test is fast."""
    base = tmp_path / "lake"
    sleeps: list[float] = []
    monkeypatch.setattr(h.time, "sleep", lambda s: sleeps.append(s))

    result = h.run(
        base_uri=str(base),
        rows_n=10_000,
        seed=42,
        ingest_date=h.date.fromisoformat("2026-05-04"),
        trigger="test",
        anomaly=Anomaly.SLOW,
    )

    assert sleeps == [h.SLOW_SLEEP_SECONDS]
    assert result.validation_passed is True
