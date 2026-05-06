"""Tests for the loan_drawdowns generator.

- `make_rows` unit tests: full-vs-partial draw distribution, schema,
  delay window.
- `handler.run` end-to-end: chain customers→apps→bureau→decisions
  →drawdowns, FK consistency on approved subset.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from lambdas.loan_drawdown_generator import handler as h
from lambdas.loan_drawdown_generator.generator import (
    DRAW_DELAY_HOURS_MAX,
    make_rows,
)
from lambdas.loan_drawdown_generator.schema import LOAN_DRAWDOWNS_SCHEMA
from lambdas.shared.parquet_writer import read_parquet, rows_to_table


def _approved(n: int, *, approved: float = 10_000, apr: float = 8.0, term: int = 36) -> list[dict]:
    decided_at = datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)
    return [
        {
            "decision_id": str(uuid4()),
            "application_id": str(uuid4()),
            "customer_id": str(uuid4()),
            "approved_amount": Decimal(str(approved)),
            "apr_pct": Decimal(str(apr)),
            "term_months": term,
            "decided_at": decided_at,
        }
        for _ in range(n)
    ]


def test_make_rows_one_per_approved():
    rows = make_rows(_approved(50), seed=1)
    assert len(rows) == 50


def test_make_rows_raises_on_empty():
    with pytest.raises(ValueError):
        make_rows([], seed=1)


def test_full_draw_share_is_about_70_percent():
    rows = make_rows(_approved(2000), seed=1)
    full = sum(1 for r in rows if r["drawn_amount"] == r["approved_amount"])
    share = full / len(rows)
    assert 0.62 <= share <= 0.78  # spec 0.70 ± seed jitter


def test_partial_draws_within_30_to_99_pct():
    rows = make_rows(_approved(500, approved=10_000), seed=1)
    for r in rows:
        ratio = float(r["drawn_amount"]) / float(r["approved_amount"])
        if ratio < 1.0:
            assert 0.30 <= ratio <= 0.99


def test_account_last4_is_4_digits():
    rows = make_rows(_approved(200), seed=1)
    for r in rows:
        assert len(r["account_last4"]) == 4
        assert r["account_last4"].isdigit()


def test_disbursed_at_within_48_hours():
    rows = make_rows(_approved(500), seed=1)
    decided_at = datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)
    for r in rows:
        delta = r["disbursed_at"] - decided_at
        assert timedelta(0) <= delta <= timedelta(hours=DRAW_DELAY_HOURS_MAX)


def test_drawn_never_exceeds_approved():
    rows = make_rows(_approved(500), seed=1)
    for r in rows:
        assert r["drawn_amount"] <= r["approved_amount"]


def test_schema_roundtrips():
    rows = make_rows(_approved(30), seed=1)
    table = rows_to_table(rows, LOAN_DRAWDOWNS_SCHEMA)
    assert table.num_rows == 30
    assert table.schema.equals(LOAN_DRAWDOWNS_SCHEMA, check_metadata=False)


def test_handler_raises_when_no_decisions(tmp_path):
    with pytest.raises(h.ParentNotFound):
        h.run(
            base_uri=str(tmp_path / "lake"),
            seed=1,
            ingest_date=date(2026, 5, 4),
            trigger="test",
        )


def test_handler_chains_e2e(tmp_path, monkeypatch):
    """customers → apps → bureau → decisions → drawdowns; FKs consistent."""
    from lambdas.credit_bureau_pulls_generator import handler as bh
    from lambdas.customer_generator import handler as ch
    from lambdas.loan_application_generator import handler as lh
    from lambdas.loan_decision_generator import handler as dh

    monkeypatch.setattr(ch, "MIN_ROWS", 1)
    monkeypatch.setattr(lh, "MIN_ROWS", 1)
    monkeypatch.setattr(bh, "MIN_ROWS", 1)
    monkeypatch.setattr(dh, "MIN_ROWS", 1)
    monkeypatch.setattr(h, "MIN_ROWS", 1)
    base = tmp_path / "lake"
    ingest = date(2026, 5, 4)

    ch.run(base_uri=str(base), rows_n=200, seed=1, ingest_date=ingest, trigger="t")
    lh.run(base_uri=str(base), rows_n=200, seed=2, ingest_date=ingest, trigger="t")
    bh.run(base_uri=str(base), seed=3, ingest_date=ingest, trigger="t")
    decision_result = dh.run(base_uri=str(base), seed=4, ingest_date=ingest, trigger="t")

    result = h.run(base_uri=str(base), seed=5, ingest_date=ingest, trigger="t")
    assert result.validation_passed is True
    assert result.rows == decision_result.approve_count
    assert result.rows > 0

    drawdowns_table = read_parquet(result.parquet_uri)
    drawdown_decision_ids = set(drawdowns_table.column("decision_id").to_pylist())

    decisions_partition = base / "raw" / "loan_decisions" / f"ingest_date={ingest.isoformat()}"
    decisions_table = read_parquet(str(next(decisions_partition.glob("*.parquet"))))
    approved_decision_ids = {
        d
        for d, dec in zip(
            decisions_table.column("decision_id").to_pylist(),
            decisions_table.column("decision").to_pylist(),
        )
        if dec == "approved"
    }
    assert drawdown_decision_ids == approved_decision_ids
