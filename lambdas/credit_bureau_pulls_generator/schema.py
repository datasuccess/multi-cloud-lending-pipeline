"""pyarrow schema for the `credit_bureau_pulls` source.

One bureau pull per loan application. Schema is the contract — any change
here is a deliberate generator-version bump and a Phase 5 dbt source-test
update."""

from __future__ import annotations

import pyarrow as pa

CREDIT_BUREAU_PULLS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("pull_id", pa.string(), nullable=False),
        pa.field("application_id", pa.string(), nullable=False),
        pa.field("customer_id", pa.string(), nullable=False),
        pa.field("bureau_name", pa.string(), nullable=False),
        pa.field("bureau_score", pa.int16(), nullable=False),
        pa.field("hard_inquiry", pa.bool_(), nullable=False),
        pa.field("tradelines_count", pa.int16(), nullable=False),
        pa.field("delinquencies_count", pa.int16(), nullable=False),
        pa.field("pulled_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("_generator_version", pa.string(), nullable=False),
        pa.field("_ingest_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)
