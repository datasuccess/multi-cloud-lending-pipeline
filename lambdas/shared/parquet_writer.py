"""Parquet writer with an explicit declared schema.

Schema is a contract, not an inference. Everything money-related is
`decimal128(12, 2)`; timestamps are `timestamp[us, UTC]`; nullability is
spelled out. Pandas is intentionally not in the path.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from lambdas.shared.storage import read_bytes, write_bytes

LOAN_APPLICATIONS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("application_id", pa.string(), nullable=False),
        pa.field("customer_id", pa.string(), nullable=False),
        pa.field("applied_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("first_name", pa.string(), nullable=False),
        pa.field("last_name", pa.string(), nullable=False),
        pa.field("email", pa.string(), nullable=False),
        pa.field("phone", pa.string(), nullable=False),
        pa.field("date_of_birth", pa.date32(), nullable=False),
        pa.field("ssn", pa.string(), nullable=False),
        pa.field("gov_id_type", pa.string(), nullable=False),
        pa.field("gov_id_number", pa.string(), nullable=False),
        pa.field("street_address", pa.string(), nullable=False),
        pa.field("city", pa.string(), nullable=False),
        pa.field("state", pa.string(), nullable=False),
        pa.field("zip", pa.string(), nullable=False),
        pa.field("country", pa.string(), nullable=False),
        pa.field("ip_address", pa.string(), nullable=False),
        pa.field("user_agent", pa.string(), nullable=False),
        pa.field("amount_requested", pa.decimal128(12, 2), nullable=False),
        pa.field("term_months", pa.int8(), nullable=False),
        pa.field("purpose", pa.string(), nullable=False),
        pa.field("channel", pa.string(), nullable=False),
        pa.field("employment_status", pa.string(), nullable=False),
        pa.field("annual_income", pa.decimal128(12, 2), nullable=False),
        pa.field("existing_debt", pa.decimal128(12, 2), nullable=False),
        pa.field("referrer_source", pa.string(), nullable=True),
        pa.field("declared_purpose_text", pa.string(), nullable=True),
        pa.field("status", pa.string(), nullable=False),
        pa.field("_generator_version", pa.string(), nullable=False),
        pa.field("_ingest_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)


def rows_to_table(rows: Sequence[dict], schema: pa.Schema) -> pa.Table:
    """Build a pyarrow Table from a list of dicts, validated against schema."""
    columns = {f.name: [r.get(f.name) for r in rows] for f in schema}
    return pa.Table.from_pydict(columns, schema=schema)


def table_to_parquet_bytes(table: pa.Table, *, compression: str = "snappy") -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression=compression)
    return buf.getvalue()


def parquet_bytes_to_table(blob: bytes) -> pa.Table:
    return pq.read_table(io.BytesIO(blob))


def write_parquet(uri: str, table: pa.Table, *, compression: str = "snappy") -> int:
    """Write parquet to a URI (s3:// or local). Returns bytes written."""
    body = table_to_parquet_bytes(table, compression=compression)
    write_bytes(uri, body, content_type="application/octet-stream")
    return len(body)


def read_parquet(uri: str) -> pa.Table:
    return parquet_bytes_to_table(read_bytes(uri))


def schema_field_names(schema: pa.Schema) -> Iterable[str]:
    return [f.name for f in schema]
