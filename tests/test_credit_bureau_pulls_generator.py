"""Tests for the credit_bureau_pulls generator.

- `make_rows` unit tests: 1:1 with parent, schema, distributions, temporal.
- `handler.run` end-to-end: requires a parent loan_apps partition;
  raises ParentNotFound when missing; chains correctly when present.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from lambdas.credit_bureau_pulls_generator import handler as h
from lambdas.credit_bureau_pulls_generator.generator import (
    BUREAUS,
    make_rows,
)
from lambdas.credit_bureau_pulls_generator.schema import CREDIT_BUREAU_PULLS_SCHEMA
from lambdas.shared.parquet_writer import read_parquet, rows_to_table


def _fake_apps(n: int, *, applied_at: datetime | None = None) -> list[dict]:
    applied_at = applied_at or datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)
    return [
        {
            "application_id": str(uuid4()),
            "customer_id": str(uuid4()),
            "applied_at": applied_at,
        }
        for _ in range(n)
    ]


def test_make_rows_one_per_application():
    apps = _fake_apps(50)
    rows = make_rows(apps, seed=1)
    assert len(rows) == 50


def test_make_rows_preserves_parent_fks():
    apps = _fake_apps(20)
    rows = make_rows(apps, seed=1)
    parent_app_ids = [a["application_id"] for a in apps]
    parent_cust_ids = [a["customer_id"] for a in apps]
    assert [r["application_id"] for r in rows] == parent_app_ids
    assert [r["customer_id"] for r in rows] == parent_cust_ids


def test_make_rows_schema_roundtrips():
    apps = _fake_apps(20)
    rows = make_rows(apps, seed=1)
    table = rows_to_table(rows, CREDIT_BUREAU_PULLS_SCHEMA)
    assert table.num_rows == 20
    assert table.schema.equals(CREDIT_BUREAU_PULLS_SCHEMA, check_metadata=False)


def test_make_rows_raises_on_empty():
    with pytest.raises(ValueError):
        make_rows([], seed=1)


def test_score_in_fico_range():
    apps = _fake_apps(500)
    rows = make_rows(apps, seed=1)
    for r in rows:
        assert 300 <= r["bureau_score"] <= 850


def test_score_median_is_fico_ish():
    apps = _fake_apps(2000)
    rows = make_rows(apps, seed=1)
    scores = sorted(r["bureau_score"] for r in rows)
    median = scores[len(scores) // 2]
    # Beta(8, 4) scaled to [300, 850] → median ≈ 670–720.
    assert 650 <= median <= 740


def test_pulled_at_after_applied_at():
    applied_at = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    apps = _fake_apps(100, applied_at=applied_at)
    rows = make_rows(apps, seed=1)
    for r in rows:
        delta = r["pulled_at"] - applied_at
        assert timedelta(seconds=60) <= delta <= timedelta(minutes=30)


def test_hard_inquiry_mostly_true():
    apps = _fake_apps(500)
    rows = make_rows(apps, seed=1)
    share = sum(1 for r in rows if r["hard_inquiry"]) / len(rows)
    assert share > 0.90  # spec says 95% with seed jitter


def test_bureau_distribution_covers_all_three():
    apps = _fake_apps(500)
    rows = make_rows(apps, seed=1)
    seen = {r["bureau_name"] for r in rows}
    assert seen == set(BUREAUS)


def test_delinquencies_correlate_inversely_with_score():
    apps = _fake_apps(2000)
    rows = make_rows(apps, seed=1)
    low = [r["delinquencies_count"] for r in rows if r["bureau_score"] < 580]
    high = [r["delinquencies_count"] for r in rows if r["bureau_score"] >= 740]
    if low and high:
        assert (sum(low) / len(low)) > (sum(high) / len(high))


def test_handler_raises_when_no_parent(tmp_path):
    base = tmp_path / "lake"
    with pytest.raises(h.ParentNotFound):
        h.run(
            base_uri=str(base),
            seed=1,
            ingest_date=date(2026, 5, 4),
            trigger="test",
        )


def test_handler_chains_after_loan_apps(tmp_path, monkeypatch):
    """Run customers → loan_apps → bureau and verify FK consistency."""
    from lambdas.customer_generator import handler as ch
    from lambdas.loan_application_generator import handler as lh
    from lambdas.shared.parent_partition import read_parent_columns

    monkeypatch.setattr(ch, "MIN_ROWS", 1)
    monkeypatch.setattr(lh, "MIN_ROWS", 1)
    monkeypatch.setattr(h, "MIN_ROWS", 1)
    base = tmp_path / "lake"

    ch.run(
        base_uri=str(base),
        rows_n=200,
        seed=1,
        ingest_date=date(2026, 5, 4),
        trigger="test",
    )
    la_result = lh.run(
        base_uri=str(base),
        rows_n=200,
        seed=2,
        ingest_date=date(2026, 5, 4),
        trigger="test",
    )

    result = h.run(
        base_uri=str(base),
        seed=3,
        ingest_date=date(2026, 5, 4),
        trigger="test",
    )
    assert result.validation_passed is True
    assert result.rows == 200
    assert result.parent_partition_uri.endswith("loan_applications/ingest_date=2026-05-04")

    bureau_table = read_parquet(result.parquet_uri)
    bureau_app_ids = set(bureau_table.column("application_id").to_pylist())

    apps_table = read_parquet(la_result.parquet_uri)
    apps_app_ids = set(apps_table.column("application_id").to_pylist())

    # Every bureau row must reference an application that exists.
    assert bureau_app_ids == apps_app_ids
