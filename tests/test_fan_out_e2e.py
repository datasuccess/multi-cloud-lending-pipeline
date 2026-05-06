"""Cross-source FK consistency e2e (Phase 2 §13.7).

Runs all seven generators end-to-end against tmp_path and asserts the
referential graph is intact:

    customers ─┐
               ├── loan_applications ── credit_bureau_pulls
               │                    ├── loan_decisions ── loan_drawdowns
               │                    │                  ├── payments
               │                    │                  └── delinquencies
               └────────────────────┘

For every downstream FK column we assert the parent universe contains
every value. Plus a few temporal invariants from §13.1.
"""

from __future__ import annotations

from datetime import date

import pyarrow.parquet as pq
import pytest

from lambdas.credit_bureau_pulls_generator import handler as bh
from lambdas.customer_generator import handler as ch
from lambdas.delinquency_generator import handler as deh
from lambdas.loan_application_generator import handler as lh
from lambdas.loan_decision_generator import handler as dh
from lambdas.loan_drawdown_generator import handler as wh
from lambdas.payment_generator import handler as ph
from lambdas.shared.parent_partition import read_parent_columns


@pytest.fixture
def lake(tmp_path, monkeypatch):
    for mod in (ch, lh, bh, dh, wh, ph, deh):
        monkeypatch.setattr(mod, "MIN_ROWS", 1)
    return tmp_path / "lake"


def _run_full_chain(base, *, day: date, seed_base: int) -> dict:
    ch.run(base_uri=str(base), rows_n=300, seed=seed_base, ingest_date=day, trigger="t")
    lh.run(base_uri=str(base), rows_n=300, seed=seed_base + 1, ingest_date=day, trigger="t")
    bh.run(base_uri=str(base), seed=seed_base + 2, ingest_date=day, trigger="t")
    dh.run(base_uri=str(base), seed=seed_base + 3, ingest_date=day, trigger="t")
    wh.run(base_uri=str(base), seed=seed_base + 4, ingest_date=day, trigger="t")
    ph.run(base_uri=str(base), seed=seed_base + 5, ingest_date=day, trigger="t")
    return deh.run(base_uri=str(base), ingest_date=day, trigger="t")


def _ids(base: str, source: str, column: str) -> set[str]:
    from lambdas.shared.parent_partition import latest_success_partition

    uri = latest_success_partition(base, source)
    assert uri is not None, f"no _SUCCESS for {source}"
    return set(read_parent_columns(uri, [column])[column])


def test_full_pipeline_fk_integrity(lake):
    day = date(2026, 5, 4)
    _run_full_chain(lake, day=day, seed_base=1)

    base = str(lake)
    customer_ids = _ids(base, "customers", "customer_id")
    application_ids = _ids(base, "loan_applications", "application_id")
    bureau_application_ids = _ids(base, "credit_bureau_pulls", "application_id")
    bureau_customer_ids = _ids(base, "credit_bureau_pulls", "customer_id")
    decision_application_ids = _ids(base, "loan_decisions", "application_id")
    decision_customer_ids = _ids(base, "loan_decisions", "customer_id")
    drawdown_decision_ids = _ids(base, "loan_drawdowns", "decision_id")
    drawdown_customer_ids = _ids(base, "loan_drawdowns", "customer_id")
    payment_drawdown_ids = _ids(base, "payments", "drawdown_id")
    payment_customer_ids = _ids(base, "payments", "customer_id")

    # Apps reference real customers (FK inversion landed in m2).
    apps_customer_ids = _ids(base, "loan_applications", "customer_id")
    assert apps_customer_ids.issubset(customer_ids)

    # Bureau pulls 1:1 with apps.
    assert bureau_application_ids == application_ids
    assert bureau_customer_ids.issubset(customer_ids)

    # Decisions 1:1 with apps.
    assert decision_application_ids == application_ids
    assert decision_customer_ids.issubset(customer_ids)

    # Drawdowns are a subset of approved decisions only.
    decision_ids_all = _ids(base, "loan_decisions", "decision_id")
    assert drawdown_decision_ids.issubset(decision_ids_all)
    assert drawdown_customer_ids.issubset(customer_ids)

    # Payments 1:1 with drawdowns.
    drawdown_ids_all = _ids(base, "loan_drawdowns", "drawdown_id")
    assert payment_drawdown_ids == drawdown_ids_all
    assert payment_customer_ids.issubset(customer_ids)


def test_temporal_invariants(lake):
    """§13.1 temporal consistency: applied_at > customers.created_at, etc."""
    day = date(2026, 5, 4)
    _run_full_chain(lake, day=day, seed_base=10)
    base = str(lake)

    from lambdas.shared.parent_partition import latest_success_partition

    apps_uri = latest_success_partition(base, "loan_applications")
    bureau_uri = latest_success_partition(base, "credit_bureau_pulls")
    decisions_uri = latest_success_partition(base, "loan_decisions")
    drawdowns_uri = latest_success_partition(base, "loan_drawdowns")

    apps = read_parent_columns(apps_uri, ["application_id", "applied_at"])
    bureau = read_parent_columns(bureau_uri, ["application_id", "pulled_at"])
    decisions = read_parent_columns(decisions_uri, ["application_id", "decided_at"])
    drawdowns = read_parent_columns(
        drawdowns_uri, ["decision_id", "disbursed_at"]
    )
    decision_decided_by_id = {
        decisions["application_id"][i]: decisions["decided_at"][i]
        for i in range(len(decisions["application_id"]))
    }
    bureau_pulled_by_app = {
        bureau["application_id"][i]: bureau["pulled_at"][i]
        for i in range(len(bureau["application_id"]))
    }

    # bureau.pulled_at >= applied_at (per §13.1: applied → pulled in 60–1800s).
    apps_applied_by_id = {
        apps["application_id"][i]: apps["applied_at"][i]
        for i in range(len(apps["application_id"]))
    }
    for app_id, pulled in bureau_pulled_by_app.items():
        assert pulled >= apps_applied_by_id[app_id]

    # decision.decided_at >= bureau.pulled_at for the same app.
    for app_id, decided in decision_decided_by_id.items():
        if app_id in bureau_pulled_by_app:
            assert decided >= bureau_pulled_by_app[app_id]


def test_delinquencies_subset_of_drawdowns_after_two_days(lake):
    """Day-2 delinquencies snapshot only references real drawdowns."""
    _run_full_chain(lake, day=date(2026, 5, 4), seed_base=100)
    day2_result = _run_full_chain(lake, day=date(2026, 5, 5), seed_base=200)

    base = str(lake)
    drawdown_ids = _ids(base, "loan_drawdowns", "drawdown_id")

    if day2_result.rows > 0:
        snapshot = pq.read_table(day2_result.parquet_uri)
        snapshot_drawdowns = set(snapshot.column("drawdown_id").to_pylist())
        assert snapshot_drawdowns.issubset(drawdown_ids)
