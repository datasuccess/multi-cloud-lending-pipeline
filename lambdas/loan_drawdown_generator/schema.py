"""pyarrow schema for the `loan_drawdowns` source.

One row per *approved* loan that the customer actually draws. Schema is
the contract — change requires a generator-version bump.
"""

from __future__ import annotations

import pyarrow as pa

LOAN_DRAWDOWNS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("drawdown_id", pa.string(), nullable=False),
        pa.field("decision_id", pa.string(), nullable=False),
        pa.field("application_id", pa.string(), nullable=False),
        pa.field("customer_id", pa.string(), nullable=False),
        pa.field("drawn_amount", pa.decimal128(12, 2), nullable=False),
        pa.field("approved_amount", pa.decimal128(12, 2), nullable=False),
        pa.field("apr_pct", pa.decimal128(5, 2), nullable=False),
        pa.field("term_months", pa.int16(), nullable=False),
        pa.field("account_last4", pa.string(), nullable=False),
        pa.field("disbursed_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("_generator_version", pa.string(), nullable=False),
        pa.field("_ingest_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)
