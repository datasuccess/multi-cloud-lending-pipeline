"""Parent-partition discovery for cross-source FK lookup.

Each downstream generator (decisions, drawdowns, payments, delinquencies,
etc.) needs the latest `_SUCCESS`'d partition of its parent source to
sample real FK values from. Two ways to find it:

1. **Ledger fast-path** — the runs ledger already records every successful
   run. One `s3:GetObject` against the latest day's ledger row gives us
   the partition URI. Cheapest.
2. **S3 LIST fallback** — `list_objects_v2` filtered to keys ending in
   `_SUCCESS`. Single API call, walks all `ingest_date=` prefixes for the
   source. Used when the ledger is unavailable or the parent's ledger row
   was not written for some reason.

The fallback is the source of truth; the fast-path is just an optimization.

The module also exposes a small helper for sampling FK values from a
parent parquet file without loading it fully into memory when avoidable.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import boto3
import pyarrow.parquet as pq

from lambdas.shared.storage import join, parse_uri, read_bytes


def _s3_client():
    return boto3.client("s3")


def _partition_prefix(base_uri: str, source: str) -> str:
    return join(base_uri, "raw", source)


def latest_success_partition(
    base_uri: str,
    source: str,
    *,
    before: date | None = None,
) -> str | None:
    """Return the URI of the latest `_SUCCESS`'d partition for `source`.

    If `before` is provided, only partitions with `ingest_date < before` are
    considered (used by the customer generator when sampling its own prior
    partitions on a re-run).

    Returns None if no `_SUCCESS` exists yet — the caller decides whether
    that's a fatal error or a fallback signal (Phase 1 standalone, etc.).
    """
    parsed = parse_uri(base_uri)
    if parsed.is_s3:
        return _latest_success_s3(parsed.bucket, parsed.key, source, before=before)
    return _latest_success_local(parsed.key, source, before=before)


def _raw_prefix(base_key: str, source: str) -> str:
    stem = base_key.rstrip("/")
    return f"{stem}/raw/{source}/" if stem else f"raw/{source}/"


def _latest_success_s3(
    bucket: str, base_key: str, source: str, *, before: date | None
) -> str | None:
    prefix = _raw_prefix(base_key, source)
    paginator = _s3_client().get_paginator("list_objects_v2")
    candidates: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if not key.endswith("/_SUCCESS"):
                continue
            ingest_date = _ingest_date_from_key(key)
            if ingest_date is None:
                continue
            if before is not None and ingest_date >= before:
                continue
            candidates.append(key)
    if not candidates:
        return None
    candidates.sort()
    latest = candidates[-1]
    partition_key = latest[: -len("/_SUCCESS")]
    return f"s3://{bucket}/{partition_key}"


def _latest_success_local(
    base_path: str, source: str, *, before: date | None
) -> str | None:
    root = Path(base_path) / "raw" / source
    if not root.exists():
        return None
    candidates: list[Path] = []
    for marker in root.rglob("_SUCCESS"):
        ingest_date = _ingest_date_from_key(str(marker))
        if ingest_date is None:
            continue
        if before is not None and ingest_date >= before:
            continue
        candidates.append(marker)
    if not candidates:
        return None
    candidates.sort()
    return str(candidates[-1].parent)


def _ingest_date_from_key(key: str) -> date | None:
    for part in key.split("/"):
        if part.startswith("ingest_date="):
            try:
                return date.fromisoformat(part.split("=", 1)[1])
            except ValueError:
                return None
    return None


def all_success_partitions_through(
    base_uri: str, source: str, *, through: date
) -> list[str]:
    """All `_SUCCESS`'d partition URIs for `source` with `ingest_date <= through`.

    Used by snapshot-style generators (delinquencies) that aggregate across
    the full history of a fan-out source. Returns sorted ascending by date.
    """
    parsed = parse_uri(base_uri)
    if parsed.is_s3:
        prefix = _raw_prefix(parsed.key, source)
        paginator = _s3_client().get_paginator("list_objects_v2")
        out: list[tuple[date, str]] = []
        for page in paginator.paginate(Bucket=parsed.bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                if not key.endswith("/_SUCCESS"):
                    continue
                ingest_date = _ingest_date_from_key(key)
                if ingest_date is None or ingest_date > through:
                    continue
                partition_key = key[: -len("/_SUCCESS")]
                out.append((ingest_date, f"s3://{parsed.bucket}/{partition_key}"))
        out.sort(key=lambda t: t[0])
        return [uri for _, uri in out]

    root = Path(parsed.key) / "raw" / source
    if not root.exists():
        return []
    out_local: list[tuple[date, str]] = []
    for marker in root.rglob("_SUCCESS"):
        ingest_date = _ingest_date_from_key(str(marker))
        if ingest_date is None or ingest_date > through:
            continue
        out_local.append((ingest_date, str(marker.parent)))
    out_local.sort(key=lambda t: t[0])
    return [uri for _, uri in out_local]


def list_parquet_uris(partition_uri: str) -> list[str]:
    """All `.parquet` object URIs under `partition_uri` (excluding manifests)."""
    parsed = parse_uri(partition_uri)
    if parsed.is_s3:
        prefix = parsed.key.rstrip("/") + "/"
        paginator = _s3_client().get_paginator("list_objects_v2")
        out: list[str] = []
        for page in paginator.paginate(Bucket=parsed.bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                if key.endswith(".parquet"):
                    out.append(f"s3://{parsed.bucket}/{key}")
        return sorted(out)
    root = Path(parsed.key)
    return sorted(str(p) for p in root.glob("*.parquet"))


def read_parent_columns(
    partition_uri: str, columns: Iterable[str]
) -> dict[str, list]:
    """Read selected columns across all parquet files in a partition.

    Returns a dict mapping column name → flat list of values. Used by
    downstream generators to sample FK values; we don't need pandas for
    this — pyarrow's column→pylist is enough.
    """
    cols = list(columns)
    out: dict[str, list] = {c: [] for c in cols}
    for uri in list_parquet_uris(partition_uri):
        blob = read_bytes(uri)
        import io

        table = pq.read_table(io.BytesIO(blob), columns=cols)
        for c in cols:
            out[c].extend(table.column(c).to_pylist())
    return out


def sample_fk_values(
    partition_uri: str, *, id_column: str, n: int, rng: random.Random
) -> list[str]:
    """Sample `n` FK values from `id_column` of the parent partition.

    Uses `random.choices` (with replacement) when n > len(parent), since
    real fan-out can have multiple downstream rows per parent row.
    Otherwise `random.sample` (without replacement).
    """
    cols = read_parent_columns(partition_uri, [id_column])
    pool = cols[id_column]
    if not pool:
        raise ValueError(f"Parent partition {partition_uri} has 0 rows in {id_column}")
    if n <= len(pool):
        return rng.sample(pool, k=n)
    return rng.choices(pool, k=n)


def read_ledger_latest_success(
    base_uri: str, source: str, *, ingest_date: date
) -> str | None:
    """Fast-path: scan the ledger for the most recent successful run on or
    before `ingest_date`. Returns the partition URI or None.

    Cheaper than `latest_success_partition` when the ledger is well-populated:
    reads at most ~30 small JSONL files vs. listing the whole source prefix.
    Falls back to None on any error (caller then uses the LIST path).
    """
    parsed = parse_uri(base_uri)
    if not parsed.is_s3:
        return None
    prefix = f"{parsed.key.rstrip('/')}/_pipeline_runs/source={source}/"
    paginator = _s3_client().get_paginator("list_objects_v2")
    keys: list[str] = []
    try:
        for page in paginator.paginate(Bucket=parsed.bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                if obj["Key"].endswith(".jsonl"):
                    keys.append(obj["Key"])
    except Exception:
        return None
    if not keys:
        return None
    keys.sort(reverse=True)
    for key in keys[:30]:
        try:
            body = _s3_client().get_object(Bucket=parsed.bucket, Key=key)["Body"].read()
        except Exception:
            continue
        for line in body.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("status") != "success":
                continue
            started = rec.get("started_at", "")
            try:
                run_date = date.fromisoformat(started.split("T")[0])
            except ValueError:
                continue
            if run_date > ingest_date:
                continue
            return join(
                base_uri,
                "raw",
                source,
                f"ingest_date={run_date.isoformat()}",
            )
    return None
