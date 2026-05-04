from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pyarrow as pa

from lambdas.shared.parquet_writer import (
    LOAN_APPLICATIONS_SCHEMA,
    read_parquet,
    rows_to_table,
    write_parquet,
)


def _row(**overrides) -> dict:
    base = {
        "application_id": "app-1",
        "customer_id": "cust-1",
        "applied_at": datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc),
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "phone": "+15555550101",
        "date_of_birth": date(1990, 1, 1),
        "ssn": "900-00-0000",
        "gov_id_type": "drivers_license",
        "gov_id_number": "X1234567",
        "street_address": "1 Pine St",
        "city": "Brooklyn",
        "state": "NY",
        "zip": "11201",
        "country": "US",
        "ip_address": "10.0.0.1",
        "user_agent": "Mozilla/5.0",
        "amount_requested": Decimal("12345.00"),
        "term_months": 36,
        "purpose": "debt_consolidation",
        "channel": "web",
        "employment_status": "employed",
        "annual_income": Decimal("85000.00"),
        "existing_debt": Decimal("12000.00"),
        "referrer_source": "google",
        "declared_purpose_text": "Pay off cards",
        "status": "submitted",
        "_generator_version": "loan_app/0.1.0",
        "_ingest_at": datetime(2026, 5, 4, 3, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def test_schema_has_29_fields():
    assert len(LOAN_APPLICATIONS_SCHEMA) == 30  # 28 business + 2 lineage = 30 total
    # money columns are decimals
    assert LOAN_APPLICATIONS_SCHEMA.field("amount_requested").type == pa.decimal128(12, 2)
    assert LOAN_APPLICATIONS_SCHEMA.field("annual_income").type == pa.decimal128(12, 2)
    assert LOAN_APPLICATIONS_SCHEMA.field("existing_debt").type == pa.decimal128(12, 2)
    # timestamps are us, UTC
    assert LOAN_APPLICATIONS_SCHEMA.field("applied_at").type == pa.timestamp("us", tz="UTC")
    # nullable: only referrer_source and declared_purpose_text
    nullable = {f.name for f in LOAN_APPLICATIONS_SCHEMA if f.nullable}
    assert nullable == {"referrer_source", "declared_purpose_text"}


def test_rows_to_table_round_trip(tmp_path):
    rows = [_row(application_id=f"app-{i}", customer_id=f"cust-{i}") for i in range(5)]
    table = rows_to_table(rows, LOAN_APPLICATIONS_SCHEMA)
    target = str(tmp_path / "out.parquet")
    bytes_written = write_parquet(target, table)
    assert bytes_written > 0

    table_back = read_parquet(target)
    assert table_back.num_rows == 5
    assert table_back.schema.equals(LOAN_APPLICATIONS_SCHEMA, check_metadata=False)
    assert table_back.column("amount_requested")[0].as_py() == Decimal("12345.00")


def test_nullable_columns_accept_none(tmp_path):
    rows = [_row(referrer_source=None, declared_purpose_text=None)]
    table = rows_to_table(rows, LOAN_APPLICATIONS_SCHEMA)
    target = str(tmp_path / "n.parquet")
    write_parquet(target, table)
    back = read_parquet(target)
    assert back.column("referrer_source")[0].as_py() is None
    assert back.column("declared_purpose_text")[0].as_py() is None
