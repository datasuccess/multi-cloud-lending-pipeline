"""pyarrow schema for the `delinquencies` source.

Daily snapshot of drawdowns whose cumulative scheduled-vs-actual gap is
positive as of `as_of_date`. Derived from drawdowns + payments — no
randomness in this generator.
"""

from __future__ import annotations

import pyarrow as pa

DELINQUENCIES_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("snapshot_id", pa.string(), nullable=False),
        pa.field("drawdown_id", pa.string(), nullable=False),
        pa.field("customer_id", pa.string(), nullable=False),
        pa.field("dpd_days", pa.int16(), nullable=False),
        pa.field("dpd_bucket", pa.string(), nullable=False),
        pa.field("outstanding_principal", pa.decimal128(12, 2), nullable=False),
        pa.field("as_of_date", pa.date32(), nullable=False),
        pa.field("_generator_version", pa.string(), nullable=False),
        pa.field("_ingest_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)
