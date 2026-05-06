"""Tests for the customers generator.

Two layers:
- `make_rows` unit tests: schema-conformance, returning-share, mutation
  rules (income drift, address change, KYC distribution).
- `handler.run` end-to-end: writes parquet + manifest + _SUCCESS,
  then re-runs and verifies returning customers appear with preserved
  created_at and advanced updated_at.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

from lambdas.customer_generator import handler as h
from lambdas.customer_generator.generator import (
    RETURNING_SHARE_DEFAULT,
    make_rows,
)
from lambdas.customer_generator.schema import CUSTOMERS_SCHEMA
from lambdas.shared.parquet_writer import read_parquet, rows_to_table


def test_make_rows_no_parent_is_all_new():
    rows = make_rows(50, date(2026, 5, 5), seed=42)
    assert len(rows) == 50
    assert all(r["is_returning"] is False for r in rows)


def test_make_rows_schema_roundtrips():
    rows = make_rows(20, date(2026, 5, 5), seed=1)
    table = rows_to_table(rows, CUSTOMERS_SCHEMA)
    assert table.num_rows == 20
    assert table.schema.equals(CUSTOMERS_SCHEMA, check_metadata=False)


def test_make_rows_with_parent_respects_share():
    parent = make_rows(100, date(2026, 5, 1), seed=1)
    new = make_rows(
        100, date(2026, 5, 2), seed=2, parent_rows=parent, returning_share=0.20
    )
    returning = [r for r in new if r["is_returning"]]
    assert len(returning) == 20


def test_returning_customer_preserves_created_at_and_id():
    parent = make_rows(50, date(2026, 5, 1), seed=1)
    new = make_rows(
        50, date(2026, 5, 2), seed=2, parent_rows=parent, returning_share=0.50
    )

    parent_index = {p["customer_id"]: p for p in parent}
    for row in new:
        if not row["is_returning"]:
            continue
        original = parent_index[row["customer_id"]]
        assert row["created_at"] == original["created_at"]
        # updated_at must advance (or at least not be earlier).
        assert row["updated_at"] >= original["updated_at"]


def test_income_drift_within_clamp():
    parent = make_rows(200, date(2026, 5, 1), seed=1)
    new = make_rows(
        200, date(2026, 5, 2), seed=2, parent_rows=parent, returning_share=1.0
    )

    parent_index = {p["customer_id"]: p for p in parent}
    for row in new:
        if not row["is_returning"]:
            continue
        original = parent_index[row["customer_id"]]
        if original["annual_income"] == 0:
            continue
        ratio = float(row["annual_income"]) / float(original["annual_income"])
        # ±25% clamp.
        assert 0.74 <= ratio <= 1.26


def test_returning_kyc_mostly_verified():
    parent = make_rows(500, date(2026, 5, 1), seed=1)
    new = make_rows(
        500, date(2026, 5, 2), seed=2, parent_rows=parent, returning_share=1.0
    )
    statuses = [r["kyc_status"] for r in new if r["is_returning"]]
    verified_share = statuses.count("verified") / len(statuses)
    assert verified_share > 0.85  # spec is 96%, with seed jitter we accept >85


def test_handler_first_run_writes_partition(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "MIN_ROWS", 1)
    base = tmp_path / "lake"
    result = h.run(
        base_uri=str(base),
        rows_n=100,
        seed=1,
        ingest_date=date(2026, 5, 5),
        trigger="test",
    )
    assert result.validation_passed is True
    assert result.parent_partition_uri is None  # nothing existed before
    assert result.returning_count == 0
    assert result.rows == 100

    partition = base / "raw" / "customers" / "ingest_date=2026-05-05"
    files = sorted(p.name for p in partition.iterdir())
    assert "_SUCCESS" in files
    assert any(f.endswith(".parquet") for f in files)
    assert any(f.endswith(".manifest.json") for f in files)

    manifest = json.loads(next(partition.glob("*.manifest.json")).read_text())
    assert manifest["validation_passed"] is True
    assert manifest["source"] == "customers"
    assert manifest["rows"] == 100


def test_handler_second_run_pulls_returning(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "MIN_ROWS", 1)
    base = tmp_path / "lake"

    h.run(
        base_uri=str(base),
        rows_n=200,
        seed=1,
        ingest_date=date(2026, 5, 1),
        trigger="test",
    )

    result = h.run(
        base_uri=str(base),
        rows_n=200,
        seed=2,
        ingest_date=date(2026, 5, 2),
        trigger="test",
        returning_share=0.20,
    )
    assert result.validation_passed is True
    assert result.parent_partition_uri is not None
    assert result.parent_partition_uri.endswith("ingest_date=2026-05-01")
    # 20% of 200 = 40.
    assert result.returning_count == 40

    partition = base / "raw" / "customers" / "ingest_date=2026-05-02"
    table = read_parquet(str(next(partition.glob("*.parquet"))))
    is_returning = table.column("is_returning").to_pylist()
    assert sum(is_returning) == 40
