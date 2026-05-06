"""Tests for the loan_decisions generator.

Two layers:
- `make_rows` unit tests: rule-engine outputs (approve rates by band,
  hard DTI rule, APR ranges, schema conformance).
- `handler.run` end-to-end: full chain customers → loan_apps → bureau
  → decisions, with FK consistency assertions.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from lambdas.loan_decision_generator import handler as h
from lambdas.loan_decision_generator.generator import (
    APPROVE_RATE,
    APR_BAND,
    make_rows,
)
from lambdas.loan_decision_generator.schema import LOAN_DECISIONS_SCHEMA
from lambdas.shared.parquet_writer import read_parquet, rows_to_table


def _joined(
    *,
    score: int,
    requested: float = 10_000,
    income: float = 60_000,
    term: int = 36,
    n: int = 1,
) -> list[dict]:
    pulled_at = datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)
    return [
        {
            "application_id": str(uuid4()),
            "customer_id": str(uuid4()),
            "amount_requested": Decimal(str(requested)),
            "annual_income": Decimal(str(income)),
            "term_months": term,
            "bureau_score": score,
            "pulled_at": pulled_at,
        }
        for _ in range(n)
    ]


def test_dti_hard_rule_declines_regardless_of_score():
    """Even a super-prime borrower hits high_dti when requested > 0.5*income."""
    rows = make_rows(
        _joined(score=800, requested=50_000, income=60_000, n=200),
        seed=1,
    )
    for r in rows:
        assert r["decision"] == "declined"
        assert r["decision_reason"] == "high_dti"
        assert r["apr_pct"] is None
        assert r["approved_amount"] is None


def test_super_prime_approve_rate():
    rows = make_rows(_joined(score=800, n=2000), seed=1)
    approve = sum(1 for r in rows if r["decision"] == "approved")
    rate = approve / len(rows)
    assert rate > 0.90  # spec 98%, with referral skim ~95%; tolerate >90%


def test_subprime_decline_rate():
    rows = make_rows(_joined(score=500, n=1000), seed=1)
    approve = sum(1 for r in rows if r["decision"] == "approved")
    assert approve / len(rows) < 0.15  # spec 5%, with seed jitter <15%


def test_apr_range_super_prime():
    rows = make_rows(_joined(score=800, n=500), seed=1)
    apr = [float(r["apr_pct"]) for r in rows if r["apr_pct"] is not None]
    assert apr, "no APR-bearing decisions; super-prime should have many approvals"
    assert min(apr) >= 6.0
    assert max(apr) <= 10.0


def test_apr_range_near_prime():
    rows = make_rows(_joined(score=620, n=2000), seed=1)
    apr = [float(r["apr_pct"]) for r in rows if r["apr_pct"] is not None]
    assert apr, "no APR-bearing decisions"
    assert min(apr) >= 15.0
    assert max(apr) <= 24.0


def test_declined_rows_have_null_apr_and_amount():
    rows = make_rows(_joined(score=500, n=500), seed=1)
    for r in rows:
        if r["decision"] == "declined":
            assert r["apr_pct"] is None
            assert r["approved_amount"] is None


def test_approved_rows_have_apr_and_amount():
    rows = make_rows(_joined(score=800, n=500), seed=1)
    for r in rows:
        if r["decision"] == "approved":
            assert r["apr_pct"] is not None
            assert r["approved_amount"] is not None


def test_referred_rows_have_null_pricing():
    rows = make_rows(_joined(score=800, n=2000), seed=1)
    referred = [r for r in rows if r["decision"] == "referred"]
    assert referred, "expected some referred decisions in 2000 super-prime apps"
    for r in referred:
        assert r["apr_pct"] is None
        assert r["approved_amount"] is None


def test_schema_roundtrips():
    joined = (
        _joined(score=800, n=20)
        + _joined(score=500, n=20)
        + _joined(score=600, n=20)
    )
    rows = make_rows(joined, seed=1)
    table = rows_to_table(rows, LOAN_DECISIONS_SCHEMA)
    assert table.num_rows == 60
    assert table.schema.equals(LOAN_DECISIONS_SCHEMA, check_metadata=False)


def test_decided_at_after_pulled_at():
    rows = make_rows(_joined(score=700, n=200), seed=1)
    pulled = datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)
    for r in rows:
        delta = (r["decided_at"] - pulled).total_seconds()
        assert 60 <= delta <= 600


def test_make_rows_raises_on_empty():
    with pytest.raises(ValueError):
        make_rows([], seed=1)


def test_handler_chains_e2e(tmp_path, monkeypatch):
    """customers → loan_apps → bureau → decisions, all FKs consistent."""
    from lambdas.credit_bureau_pulls_generator import handler as bh
    from lambdas.customer_generator import handler as ch
    from lambdas.loan_application_generator import handler as lh

    monkeypatch.setattr(ch, "MIN_ROWS", 1)
    monkeypatch.setattr(lh, "MIN_ROWS", 1)
    monkeypatch.setattr(bh, "MIN_ROWS", 1)
    monkeypatch.setattr(h, "MIN_ROWS", 1)
    base = tmp_path / "lake"
    ingest = date(2026, 5, 4)

    ch.run(base_uri=str(base), rows_n=200, seed=1, ingest_date=ingest, trigger="t")
    lh.run(base_uri=str(base), rows_n=200, seed=2, ingest_date=ingest, trigger="t")
    bh.run(base_uri=str(base), seed=3, ingest_date=ingest, trigger="t")
    result = h.run(base_uri=str(base), seed=4, ingest_date=ingest, trigger="t")

    assert result.validation_passed is True
    assert result.rows == 200
    assert result.approve_count + result.decline_count + result.referred_count == 200

    decisions = read_parquet(result.parquet_uri)
    decisions_app_ids = set(decisions.column("application_id").to_pylist())

    apps_partition = base / "raw" / "loan_applications" / f"ingest_date={ingest.isoformat()}"
    apps_table = read_parquet(str(next(apps_partition.glob("*.parquet"))))
    apps_app_ids = set(apps_table.column("application_id").to_pylist())

    assert decisions_app_ids == apps_app_ids


def test_handler_raises_when_bureau_missing(tmp_path, monkeypatch):
    """If only apps is present (no bureau), decisions can't run."""
    from lambdas.customer_generator import handler as ch
    from lambdas.loan_application_generator import handler as lh

    monkeypatch.setattr(ch, "MIN_ROWS", 1)
    monkeypatch.setattr(lh, "MIN_ROWS", 1)
    base = tmp_path / "lake"
    ingest = date(2026, 5, 4)

    ch.run(base_uri=str(base), rows_n=200, seed=1, ingest_date=ingest, trigger="t")
    lh.run(base_uri=str(base), rows_n=200, seed=2, ingest_date=ingest, trigger="t")

    with pytest.raises(h.ParentNotFound):
        h.run(base_uri=str(base), seed=3, ingest_date=ingest, trigger="t")
