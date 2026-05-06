"""pyarrow schema for the `customers` source.

Schema is the contract. The hash baked from this object is what the
post-write validator compares against — any change here means a deliberate
schema bump and a Phase 5 dbt source-test update."""

from __future__ import annotations

import pyarrow as pa

CUSTOMERS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("customer_id", pa.string(), nullable=False),
        pa.field("first_name", pa.string(), nullable=False),
        pa.field("last_name", pa.string(), nullable=False),
        pa.field("email", pa.string(), nullable=False),
        pa.field("phone", pa.string(), nullable=False),
        pa.field("date_of_birth", pa.date32(), nullable=False),
        pa.field("address_line1", pa.string(), nullable=False),
        pa.field("city", pa.string(), nullable=False),
        pa.field("state", pa.string(), nullable=False),
        pa.field("zip", pa.string(), nullable=False),
        pa.field("kyc_status", pa.string(), nullable=False),
        pa.field("employment_status", pa.string(), nullable=False),
        pa.field("annual_income", pa.decimal128(12, 2), nullable=False),
        pa.field("created_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("updated_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("is_returning", pa.bool_(), nullable=False),
        pa.field("_generator_version", pa.string(), nullable=False),
        pa.field("_ingest_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)
