"""Post-write validation — read parquet back, check shape against schema.

A job that writes 0 rows and says "success" is the canonical 3am page.
Every successful run must pass this check before `_SUCCESS` is written.
See docs/monitoring.md §5.3.
"""

from __future__ import annotations

import pyarrow as pa


def validate_table(
    table: pa.Table,
    *,
    expected_rows: int,
    schema: pa.Schema,
    min_rows: int,
) -> list[str]:
    errors: list[str] = []
    if table.num_rows != expected_rows:
        errors.append(
            f"row count mismatch: got {table.num_rows}, expected {expected_rows}"
        )
    if table.num_rows < min_rows:
        errors.append(f"too few rows: {table.num_rows} < {min_rows}")
    if not table.schema.equals(schema, check_metadata=False):
        errors.append("schema drift vs declared LOAN_APPLICATIONS_SCHEMA")
    return errors
