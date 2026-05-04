from __future__ import annotations

import pyarrow as pa

from lambdas.shared.parquet_writer import LOAN_APPLICATIONS_SCHEMA
from lambdas.shared.validation import validate_table


def _empty_table(schema: pa.Schema) -> pa.Table:
    arrays = [pa.array([], type=f.type) for f in schema]
    return pa.Table.from_arrays(arrays, schema=schema)


def test_passes_on_matching_table():
    table = _empty_table(LOAN_APPLICATIONS_SCHEMA)
    # zero-row table won't pass min_rows so we lower the bar:
    errors = validate_table(
        table, expected_rows=0, schema=LOAN_APPLICATIONS_SCHEMA, min_rows=0
    )
    assert errors == []


def test_flags_row_count_mismatch():
    table = _empty_table(LOAN_APPLICATIONS_SCHEMA)
    errors = validate_table(
        table, expected_rows=10, schema=LOAN_APPLICATIONS_SCHEMA, min_rows=0
    )
    assert any("row count mismatch" in e for e in errors)


def test_flags_too_few_rows():
    table = _empty_table(LOAN_APPLICATIONS_SCHEMA)
    errors = validate_table(
        table, expected_rows=0, schema=LOAN_APPLICATIONS_SCHEMA, min_rows=10
    )
    assert any("too few rows" in e for e in errors)


def test_flags_schema_drift():
    drifted = pa.schema([pa.field("only", pa.int32())])
    table = pa.Table.from_pydict({"only": [1, 2, 3]}, schema=drifted)
    errors = validate_table(
        table, expected_rows=3, schema=LOAN_APPLICATIONS_SCHEMA, min_rows=0
    )
    assert any("schema drift" in e for e in errors)
