"""pyarrow schema for the `loan_decisions` source.

One decision per application. apr_pct and approved_amount are nullable
because declined / referred decisions don't carry pricing.
"""

from __future__ import annotations

import pyarrow as pa

LOAN_DECISIONS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("decision_id", pa.string(), nullable=False),
        pa.field("application_id", pa.string(), nullable=False),
        pa.field("customer_id", pa.string(), nullable=False),
        pa.field("decision", pa.string(), nullable=False),
        pa.field("decision_reason", pa.string(), nullable=False),
        pa.field("apr_pct", pa.decimal128(5, 2), nullable=True),
        pa.field("approved_amount", pa.decimal128(12, 2), nullable=True),
        pa.field("term_months", pa.int16(), nullable=False),
        pa.field("decided_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("_generator_version", pa.string(), nullable=False),
        pa.field("_ingest_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)
