"""Tests for the delinquencies generator.

- `make_rows` unit tests: derived snapshot logic, DPD bucketing,
  zero-gap drawdowns excluded, outstanding-principal floor.
- `handler.run` end-to-end: full chain (customers → … → payments) on
  two days; day-2 reads day-1 payments and emits derived snapshot rows.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from lambdas.delinquency_generator import handler as h
from lambdas.delinquency_generator.generator import (
    DPD_BUCKETS,
    _bucket_for,
    make_rows,
)
from lambdas.delinquency_generator.schema import DELINQUENCIES_SCHEMA
from lambdas.shared.parquet_writer import read_parquet, rows_to_table


def _drawdown(*, drawn: float = 10_000) -> dict:
    return {
        "drawdown_id": str(uuid4()),
        "customer_id": str(uuid4()),
        "drawn_amount": Decimal(str(drawn)),
    }


def _payment(
    *,
    drawdown_id: str,
    scheduled_at: date,
    scheduled: float = 300.0,
    actual: float = 0.0,
    principal: float = 0.0,
) -> dict:
    return {
        "drawdown_id": drawdown_id,
        "scheduled_amount": Decimal(str(scheduled)),
        "actual_amount": Decimal(str(actual)),
        "principal_amount": Decimal(str(principal)),
        "scheduled_at": scheduled_at,
    }


def test_bucket_boundaries():
    assert _bucket_for(1) == "1-30"
    assert _bucket_for(30) == "1-30"
    assert _bucket_for(31) == "31-60"
    assert _bucket_for(60) == "31-60"
    assert _bucket_for(61) == "61-90"
    assert _bucket_for(90) == "61-90"
    assert _bucket_for(91) == "90+"
    assert _bucket_for(365) == "90+"


def test_no_payments_history_emits_nothing():
    drawdown = _drawdown()
    rows = make_rows(
        [drawdown],
        payments_by_drawdown={},
        as_of_date=date(2026, 5, 6),
    )
    assert rows == []


def test_paid_in_full_emits_nothing():
    """Drawdown with cumulative scheduled == cumulative actual is not delinquent."""
    drawdown = _drawdown()
    history = [
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 4, 1),
            scheduled=300.0,
            actual=300.0,
            principal=250.0,
        ),
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 5, 1),
            scheduled=300.0,
            actual=300.0,
            principal=251.0,
        ),
    ]
    rows = make_rows(
        [drawdown],
        payments_by_drawdown={drawdown["drawdown_id"]: history},
        as_of_date=date(2026, 5, 6),
    )
    assert rows == []


def test_single_missed_payment_lands_in_correct_bucket():
    drawdown = _drawdown()
    history = [
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 4, 21),
            scheduled=300.0,
            actual=0.0,
            principal=0.0,
        ),
    ]
    as_of = date(2026, 5, 6)  # 15 days late
    rows = make_rows(
        [drawdown],
        payments_by_drawdown={drawdown["drawdown_id"]: history},
        as_of_date=as_of,
    )
    assert len(rows) == 1
    assert rows[0]["dpd_days"] == 15
    assert rows[0]["dpd_bucket"] == "1-30"


def test_old_anchor_lands_in_90plus_bucket():
    drawdown = _drawdown()
    history = [
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 1, 1),
            scheduled=300.0,
            actual=0.0,
        ),
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 2, 1),
            scheduled=300.0,
            actual=0.0,
        ),
    ]
    rows = make_rows(
        [drawdown],
        payments_by_drawdown={drawdown["drawdown_id"]: history},
        as_of_date=date(2026, 5, 6),
    )
    assert len(rows) == 1
    assert rows[0]["dpd_bucket"] == "90+"
    assert rows[0]["dpd_days"] >= 91


def test_overpayment_clears_anchor():
    """Once running gap returns to ≤ 0, the drawdown is no longer delinquent."""
    drawdown = _drawdown()
    history = [
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 4, 1),
            scheduled=300.0,
            actual=0.0,
        ),
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 5, 1),
            scheduled=300.0,
            actual=600.0,
            principal=550.0,
        ),
    ]
    rows = make_rows(
        [drawdown],
        payments_by_drawdown={drawdown["drawdown_id"]: history},
        as_of_date=date(2026, 5, 6),
    )
    assert rows == []


def test_outstanding_principal_floored_at_zero():
    """Approximation can over-pay principal; result should never go negative."""
    drawdown = _drawdown(drawn=1000.0)
    history = [
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 1, 1),
            scheduled=500.0,
            actual=0.0,
            principal=0.0,
        ),
        # Scenario: a partial payment somewhere with a huge over-principal.
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 2, 1),
            scheduled=500.0,
            actual=300.0,
            principal=2000.0,  # absurd, but tests the floor
        ),
    ]
    rows = make_rows(
        [drawdown],
        payments_by_drawdown={drawdown["drawdown_id"]: history},
        as_of_date=date(2026, 5, 6),
    )
    assert len(rows) == 1
    assert rows[0]["outstanding_principal"] == Decimal("0.00")


def test_dpd_days_floored_at_one():
    """Anchor ≥ as_of_date can happen with same-day data; DPD must be ≥ 1."""
    drawdown = _drawdown()
    history = [
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 5, 6),
            scheduled=300.0,
            actual=0.0,
        ),
    ]
    rows = make_rows(
        [drawdown],
        payments_by_drawdown={drawdown["drawdown_id"]: history},
        as_of_date=date(2026, 5, 6),
    )
    assert len(rows) == 1
    assert rows[0]["dpd_days"] == 1
    assert rows[0]["dpd_bucket"] == "1-30"


def test_schema_roundtrips():
    drawdown = _drawdown()
    history = [
        _payment(
            drawdown_id=drawdown["drawdown_id"],
            scheduled_at=date(2026, 4, 1),
            scheduled=300.0,
            actual=0.0,
        ),
    ]
    rows = make_rows(
        [drawdown],
        payments_by_drawdown={drawdown["drawdown_id"]: history},
        as_of_date=date(2026, 5, 6),
    )
    table = rows_to_table(rows, DELINQUENCIES_SCHEMA)
    assert table.num_rows == 1
    assert table.schema.equals(DELINQUENCIES_SCHEMA, check_metadata=False)


def test_buckets_constant_matches_spec():
    assert DPD_BUCKETS == ("1-30", "31-60", "61-90", "90+")


def test_handler_raises_when_no_drawdowns(tmp_path):
    with pytest.raises(h.ParentNotFound):
        h.run(
            base_uri=str(tmp_path / "lake"),
            ingest_date=date(2026, 5, 6),
            trigger="test",
        )


def test_handler_chains_e2e(tmp_path, monkeypatch):
    """Full chain across two days; day 2 emits a deterministic snapshot."""
    from lambdas.credit_bureau_pulls_generator import handler as bh
    from lambdas.customer_generator import handler as ch
    from lambdas.loan_application_generator import handler as lh
    from lambdas.loan_decision_generator import handler as dh
    from lambdas.loan_drawdown_generator import handler as wh
    from lambdas.payment_generator import handler as ph

    for mod in (ch, lh, bh, dh, wh, ph, h):
        monkeypatch.setattr(mod, "MIN_ROWS", 1)

    base = tmp_path / "lake"
    day1 = date(2026, 5, 4)
    day2 = date(2026, 5, 5)

    # Day 1: full chain through payments. No delinquencies yet (would need
    # at least one missed payment, which the Markov state matrix produces
    # ~3% of the time).
    ch.run(base_uri=str(base), rows_n=200, seed=1, ingest_date=day1, trigger="t")
    lh.run(base_uri=str(base), rows_n=200, seed=2, ingest_date=day1, trigger="t")
    bh.run(base_uri=str(base), seed=3, ingest_date=day1, trigger="t")
    dh.run(base_uri=str(base), seed=4, ingest_date=day1, trigger="t")
    wh.run(base_uri=str(base), seed=5, ingest_date=day1, trigger="t")
    ph.run(base_uri=str(base), seed=6, ingest_date=day1, trigger="t")

    # Day 2 — same upstream chain.
    ch.run(base_uri=str(base), rows_n=200, seed=11, ingest_date=day2, trigger="t")
    lh.run(base_uri=str(base), rows_n=200, seed=12, ingest_date=day2, trigger="t")
    bh.run(base_uri=str(base), seed=13, ingest_date=day2, trigger="t")
    dh.run(base_uri=str(base), seed=14, ingest_date=day2, trigger="t")
    wh.run(base_uri=str(base), seed=15, ingest_date=day2, trigger="t")
    ph.run(base_uri=str(base), seed=16, ingest_date=day2, trigger="t")

    result = h.run(base_uri=str(base), ingest_date=day2, trigger="t")
    assert result.validation_passed is True
    assert len(result.payments_partition_uris) == 2  # day1 + day2

    # Snapshot rows must reference real drawdowns from today's partition.
    if result.rows > 0:
        snapshot_table = read_parquet(result.parquet_uri)
        snapshot_drawdowns = set(snapshot_table.column("drawdown_id").to_pylist())
        drawdowns_uri = result.drawdowns_partition_uri
        from lambdas.shared.parent_partition import read_parent_columns

        drawdown_ids = set(
            read_parent_columns(drawdowns_uri, ["drawdown_id"])["drawdown_id"]
        )
        assert snapshot_drawdowns.issubset(drawdown_ids)

        # Every emitted row must have a valid bucket and dpd_days ≥ 1.
        for bucket in snapshot_table.column("dpd_bucket").to_pylist():
            assert bucket in DPD_BUCKETS
        for dpd in snapshot_table.column("dpd_days").to_pylist():
            assert dpd >= 1
