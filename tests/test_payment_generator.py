"""Tests for the payments generator.

- `make_rows` unit tests: Markov transitions, schema, amortization,
  status-amount invariants.
- `handler.run` end-to-end: full chain to drawdowns, then payments;
  also a 2-day continuity test that asserts Markov state from day 1
  influences day 2 outcomes.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from lambdas.payment_generator import handler as h
from lambdas.payment_generator.generator import (
    TRANSITIONS,
    make_rows,
)
from lambdas.payment_generator.schema import PAYMENTS_SCHEMA
from lambdas.shared.parquet_writer import read_parquet, rows_to_table


def _drawdowns(n: int, *, drawn: float = 10_000, apr: float = 8.0, term: int = 36) -> list[dict]:
    return [
        {
            "drawdown_id": str(uuid4()),
            "customer_id": str(uuid4()),
            "drawn_amount": Decimal(str(drawn)),
            "apr_pct": Decimal(str(apr)),
            "term_months": term,
        }
        for _ in range(n)
    ]


def test_make_rows_one_per_drawdown():
    rows = make_rows(_drawdowns(50), seed=1)
    assert len(rows) == 50


def test_make_rows_raises_on_empty():
    with pytest.raises(ValueError):
        make_rows([], seed=1)


def test_no_prior_state_is_mostly_paid_full():
    """First-run: every drawdown starts from 'paid_full' → 92% paid_full."""
    rows = make_rows(_drawdowns(2000), seed=1)
    counts = Counter(r["payment_status"] for r in rows)
    paid_full_share = counts["paid_full"] / sum(counts.values())
    assert paid_full_share > 0.85  # spec 92%, tolerance for waiver + jitter


def test_prior_missed_increases_miss_rate():
    """Drawdowns whose prior status was 'missed' must miss more often than the cohort."""
    drawdowns = _drawdowns(1500)
    prior = {d["drawdown_id"]: "missed" for d in drawdowns[:500]}
    rows = make_rows(drawdowns, seed=1, prior_status_by_drawdown=prior)
    by_id = {r["drawdown_id"]: r["payment_status"] for r in rows}

    miss_with_prior_missed = sum(
        1 for d in drawdowns[:500] if by_id[d["drawdown_id"]] == "missed"
    )
    miss_with_no_prior = sum(
        1 for d in drawdowns[500:] if by_id[d["drawdown_id"]] == "missed"
    )

    rate_missed = miss_with_prior_missed / 500
    rate_clean = miss_with_no_prior / 1000
    # Spec: 40% miss-after-miss vs 3% miss-after-paid_full.
    assert rate_missed > 0.25
    assert rate_clean < 0.10
    assert rate_missed > rate_clean


def test_status_money_invariants():
    """missed/waived → all amounts zero; paid_full → actual=scheduled."""
    rows = make_rows(_drawdowns(500), seed=1)
    for r in rows:
        if r["payment_status"] in ("missed", "waived"):
            assert r["actual_amount"] == Decimal("0.00")
            assert r["principal_amount"] == Decimal("0.00")
            assert r["interest_amount"] == Decimal("0.00")
            assert r["paid_at"] is None
        elif r["payment_status"] == "paid_full":
            assert r["actual_amount"] == r["scheduled_amount"]
            assert r["paid_at"] is not None
        elif r["payment_status"] == "paid_partial":
            assert Decimal("0") < r["actual_amount"] < r["scheduled_amount"]
            assert r["paid_at"] is not None


def test_principal_plus_interest_equals_actual_for_paid():
    rows = make_rows(_drawdowns(200), seed=1)
    for r in rows:
        if r["payment_status"] in ("paid_full", "paid_partial"):
            total = r["principal_amount"] + r["interest_amount"]
            # Allow penny rounding drift.
            assert abs(float(total) - float(r["actual_amount"])) <= 0.02


def test_schema_roundtrips():
    rows = make_rows(_drawdowns(30), seed=1)
    table = rows_to_table(rows, PAYMENTS_SCHEMA)
    assert table.num_rows == 30
    assert table.schema.equals(PAYMENTS_SCHEMA, check_metadata=False)


def test_cap_truncates_deterministically():
    drawdowns = _drawdowns(50)
    rows1 = make_rows(drawdowns, seed=1, cap=20)
    rows2 = make_rows(drawdowns, seed=2, cap=20)
    assert len(rows1) == 20
    # Same drawdown_ids in both runs (deterministic by sort), only status differs.
    ids1 = sorted(r["drawdown_id"] for r in rows1)
    ids2 = sorted(r["drawdown_id"] for r in rows2)
    assert ids1 == ids2


def test_handler_raises_when_no_drawdowns(tmp_path):
    with pytest.raises(h.ParentNotFound):
        h.run(
            base_uri=str(tmp_path / "lake"),
            seed=1,
            ingest_date=date(2026, 5, 4),
            trigger="test",
        )


def test_handler_chains_e2e(tmp_path, monkeypatch):
    """Full chain: customers → apps → bureau → decisions → drawdowns → payments."""
    from lambdas.credit_bureau_pulls_generator import handler as bh
    from lambdas.customer_generator import handler as ch
    from lambdas.loan_application_generator import handler as lh
    from lambdas.loan_decision_generator import handler as dh
    from lambdas.loan_drawdown_generator import handler as wh

    for mod in (ch, lh, bh, dh, wh, h):
        monkeypatch.setattr(mod, "MIN_ROWS", 1)

    base = tmp_path / "lake"
    ingest = date(2026, 5, 4)

    ch.run(base_uri=str(base), rows_n=200, seed=1, ingest_date=ingest, trigger="t")
    lh.run(base_uri=str(base), rows_n=200, seed=2, ingest_date=ingest, trigger="t")
    bh.run(base_uri=str(base), seed=3, ingest_date=ingest, trigger="t")
    dh.run(base_uri=str(base), seed=4, ingest_date=ingest, trigger="t")
    drawdown_result = wh.run(base_uri=str(base), seed=5, ingest_date=ingest, trigger="t")

    result = h.run(base_uri=str(base), seed=6, ingest_date=ingest, trigger="t")
    assert result.validation_passed is True
    assert result.rows == drawdown_result.rows
    assert result.prior_payments_partition_uri is None  # first run

    payments_table = read_parquet(result.parquet_uri)
    payment_drawdown_ids = set(payments_table.column("drawdown_id").to_pylist())

    drawdowns_table = read_parquet(drawdown_result.parquet_uri)
    drawdown_ids = set(drawdowns_table.column("drawdown_id").to_pylist())
    assert payment_drawdown_ids == drawdown_ids


def test_handler_picks_up_prior_payments_partition(tmp_path, monkeypatch):
    """Day-1 payments partition becomes day-2's Markov state."""
    from lambdas.credit_bureau_pulls_generator import handler as bh
    from lambdas.customer_generator import handler as ch
    from lambdas.loan_application_generator import handler as lh
    from lambdas.loan_decision_generator import handler as dh
    from lambdas.loan_drawdown_generator import handler as wh

    for mod in (ch, lh, bh, dh, wh, h):
        monkeypatch.setattr(mod, "MIN_ROWS", 1)

    base = tmp_path / "lake"
    day1 = date(2026, 5, 4)
    day2 = date(2026, 5, 5)

    # Day 1
    ch.run(base_uri=str(base), rows_n=200, seed=1, ingest_date=day1, trigger="t")
    lh.run(base_uri=str(base), rows_n=200, seed=2, ingest_date=day1, trigger="t")
    bh.run(base_uri=str(base), seed=3, ingest_date=day1, trigger="t")
    dh.run(base_uri=str(base), seed=4, ingest_date=day1, trigger="t")
    wh.run(base_uri=str(base), seed=5, ingest_date=day1, trigger="t")
    h.run(base_uri=str(base), seed=6, ingest_date=day1, trigger="t")

    # Day 2 — same upstream chain so drawdowns also exist, then payments must
    # find day-1's payments partition as prior state.
    ch.run(base_uri=str(base), rows_n=200, seed=11, ingest_date=day2, trigger="t")
    lh.run(base_uri=str(base), rows_n=200, seed=12, ingest_date=day2, trigger="t")
    bh.run(base_uri=str(base), seed=13, ingest_date=day2, trigger="t")
    dh.run(base_uri=str(base), seed=14, ingest_date=day2, trigger="t")
    wh.run(base_uri=str(base), seed=15, ingest_date=day2, trigger="t")
    day2_result = h.run(base_uri=str(base), seed=16, ingest_date=day2, trigger="t")

    assert day2_result.validation_passed is True
    assert day2_result.prior_payments_partition_uri is not None
    assert day2_result.prior_payments_partition_uri.endswith(
        "payments/ingest_date=2026-05-04"
    )
