from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from lambdas.loan_application_generator.generator import (
    GENERATOR_VERSION,
    SOURCE,
    make_rows,
)
from lambdas.shared.parquet_writer import LOAN_APPLICATIONS_SCHEMA, rows_to_table

INGEST_DATE = date(2026, 5, 4)
INGEST_AT = datetime(2026, 5, 4, 3, 0, 0, tzinfo=timezone.utc)


def test_make_rows_count():
    rows = make_rows(50, INGEST_DATE, seed=42, ingest_at=INGEST_AT)
    assert len(rows) == 50


def test_make_rows_seeded_is_deterministic():
    a = make_rows(10, INGEST_DATE, seed=7, ingest_at=INGEST_AT)
    b = make_rows(10, INGEST_DATE, seed=7, ingest_at=INGEST_AT)
    assert [r["application_id"] for r in a] != [r["application_id"] for r in b]
    # uuid4 isn't seeded — but deterministic columns should match
    assert [r["email"] for r in a] == [r["email"] for r in b]
    assert [r["amount_requested"] for r in a] == [r["amount_requested"] for r in b]


def test_make_rows_invariants():
    rows = make_rows(200, INGEST_DATE, seed=42, ingest_at=INGEST_AT)
    for r in rows:
        assert r["status"] == "submitted"
        assert r["_generator_version"] == GENERATOR_VERSION
        assert r["_ingest_at"] == INGEST_AT
        assert Decimal("1000.00") <= r["amount_requested"] <= Decimal("50000.00")
        assert r["term_months"] in {12, 24, 36, 48, 60}
        assert r["channel"] in {"web", "mobile", "branch", "partner"}
        assert r["purpose"] in {
            "debt_consolidation", "home_improvement", "auto", "medical", "other",
        }
        assert r["employment_status"] in {
            "employed", "self_employed", "unemployed", "retired",
        }
        assert r["gov_id_type"] in {"drivers_license", "passport", "state_id"}
        # applied_at must be in the 24h preceding 03:00 UTC ingest day
        assert r["applied_at"] <= INGEST_AT
        assert (INGEST_AT - r["applied_at"]).total_seconds() <= 24 * 3600 + 1


def test_rows_pass_declared_schema():
    rows = make_rows(20, INGEST_DATE, seed=1, ingest_at=INGEST_AT)
    table = rows_to_table(rows, LOAN_APPLICATIONS_SCHEMA)
    assert table.num_rows == 20
    assert table.schema.equals(LOAN_APPLICATIONS_SCHEMA, check_metadata=False)


def test_source_constant():
    assert SOURCE == "loan_applications"


def test_make_rows_zero_is_valid():
    rows = make_rows(0, INGEST_DATE, seed=1, ingest_at=INGEST_AT)
    assert rows == []
