"""Manifest sidecar + _SUCCESS marker.

Pattern (see docs/monitoring.md §5.1): write parquet → write manifest →
write _SUCCESS. The presence of `_SUCCESS` is what downstream loaders wait
for. The manifest carries enough metadata to verify shape without reading
the parquet itself.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa

from lambdas.shared.storage import write_bytes


def schema_hash(schema: pa.Schema) -> str:
    """Stable hash over `name:type:nullable` for every field."""
    parts = [f"{f.name}:{f.type}:{f.nullable}" for f in schema]
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def utc_iso(ts: datetime | None = None) -> str:
    ts = ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_manifest(
    *,
    run_id: str,
    source: str,
    generator_version: str,
    ingest_date: str,
    parquet_key: str,
    rows: int,
    bytes_: int,
    schema: pa.Schema,
    started_at: datetime,
    finished_at: datetime,
    validation_passed: bool,
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    return {
        "run_id": run_id,
        "source": source,
        "generator_version": generator_version,
        "ingest_date": ingest_date,
        "parquet_key": parquet_key,
        "rows": rows,
        "bytes": bytes_,
        "schema_hash": schema_hash(schema),
        "started_at": utc_iso(started_at),
        "finished_at": utc_iso(finished_at),
        "duration_ms": duration_ms,
        "validation_passed": validation_passed,
        "validation_errors": validation_errors or [],
    }


def write_manifest(uri: str, manifest: dict[str, Any]) -> int:
    body = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    write_bytes(uri, body, content_type="application/json")
    return len(body)


def write_success(uri: str) -> None:
    write_bytes(uri, b"", content_type="text/plain")
