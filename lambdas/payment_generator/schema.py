"""pyarrow schema for the `payments` source.

One row per scheduled repayment event. Phase 2 simplification: one
payment per drawdown per generator run (real-world = monthly schedule).
The Markov-ish state machine on payment_status is what gives Phase 5
dbt's delinquency staging non-trivial state to reduce.
"""

from __future__ import annotations

import pyarrow as pa

PAYMENTS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("payment_id", pa.string(), nullable=False),
        pa.field("drawdown_id", pa.string(), nullable=False),
        pa.field("customer_id", pa.string(), nullable=False),
        pa.field("scheduled_amount", pa.decimal128(12, 2), nullable=False),
        pa.field("actual_amount", pa.decimal128(12, 2), nullable=False),
        pa.field("principal_amount", pa.decimal128(12, 2), nullable=False),
        pa.field("interest_amount", pa.decimal128(12, 2), nullable=False),
        pa.field("payment_status", pa.string(), nullable=False),
        pa.field("scheduled_at", pa.date32(), nullable=False),
        pa.field("paid_at", pa.timestamp("us", tz="UTC"), nullable=True),
        pa.field("_generator_version", pa.string(), nullable=False),
        pa.field("_ingest_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)
