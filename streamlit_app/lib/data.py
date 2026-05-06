"""Data access for the monitoring UI.

Reads the runs ledger (JSONL under `_pipeline_runs/`) and the latest parquet
partitions directly from S3. Caches results with `st.cache_data` so flipping
between pages doesn't re-download.

The ledger is the system of record for "did the run happen, and how did it
go?" — see docs/monitoring.md §5.2.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any

import boto3
import pandas as pd
import pyarrow.parquet as pq
import streamlit as st


@st.cache_resource
def s3_client(region: str) -> Any:
    return boto3.client("s3", region_name=region)


@st.cache_resource
def cw_client(region: str) -> Any:
    return boto3.client("cloudwatch", region_name=region)


@st.cache_data(ttl=60)
def list_runs(bucket: str, region: str, source: str, days: int = 14) -> pd.DataFrame:
    """Returns the runs ledger as a DataFrame for the last N days."""
    s3 = s3_client(region)
    today = datetime.now(timezone.utc).date()
    records: list[dict[str, Any]] = []
    for offset in range(days):
        d = today - timedelta(days=offset)
        prefix = (
            f"_pipeline_runs/source={source}/"
            f"year={d.year:04d}/month={d.month:02d}/day={d.day:02d}/"
        )
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix
        ):
            for obj in page.get("Contents", []) or []:
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                for line in body.splitlines():
                    if line.strip():
                        records.append(json.loads(line))
    if not records:
        return pd.DataFrame(
            columns=[
                "run_id",
                "source",
                "started_at",
                "finished_at",
                "status",
                "rows",
                "bytes",
                "duration_ms",
                "trigger",
            ]
        )
    df = pd.DataFrame(records)
    df["started_at"] = pd.to_datetime(df["started_at"], utc=True)
    df["finished_at"] = pd.to_datetime(df["finished_at"], utc=True)
    return df.sort_values("started_at", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=60)
def list_partitions(bucket: str, region: str, source: str) -> pd.DataFrame:
    """List ingest_date partitions and whether each has a `_SUCCESS` marker."""
    s3 = s3_client(region)
    prefix = f"raw/{source}/"
    rows: list[dict[str, Any]] = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix, Delimiter="/"
    ):
        for cp in page.get("CommonPrefixes", []) or []:
            part = cp["Prefix"].split("ingest_date=", 1)[-1].rstrip("/")
            success_key = f"{cp['Prefix']}_SUCCESS"
            try:
                s3.head_object(Bucket=bucket, Key=success_key)
                has_success = True
            except s3.exceptions.ClientError:
                has_success = False
            size = 0
            objects = 0
            for sub in s3.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=cp["Prefix"]
            ):
                for obj in sub.get("Contents", []) or []:
                    if obj["Key"].endswith(".parquet"):
                        size += obj["Size"]
                        objects += 1
            rows.append(
                {
                    "ingest_date": part,
                    "has_success": has_success,
                    "parquet_files": objects,
                    "parquet_bytes": size,
                }
            )
    return pd.DataFrame(rows).sort_values("ingest_date", ascending=False)


@st.cache_data(ttl=120)
def latest_parquet(bucket: str, region: str, source: str) -> pd.DataFrame | None:
    """Read the most recent successful partition's parquet for KPI computation."""
    parts = list_partitions(bucket, region, source)
    parts = parts[parts["has_success"]]
    if parts.empty:
        return None
    latest = parts.iloc[0]["ingest_date"]
    s3 = s3_client(region)
    prefix = f"raw/{source}/ingest_date={latest}/"
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(
            o["Key"] for o in page.get("Contents", []) or [] if o["Key"].endswith(".parquet")
        )
    if not keys:
        return None
    frames: list[pd.DataFrame] = []
    for key in keys:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        frames.append(pq.read_table(BytesIO(body)).to_pandas())
    df = pd.concat(frames, ignore_index=True)
    df.attrs["ingest_date"] = latest
    return df


@st.cache_data(ttl=30)
def alarm_states(region: str, names: list[str]) -> pd.DataFrame:
    """Snapshot of alarm state for the home page."""
    cw = cw_client(region)
    resp = cw.describe_alarms(AlarmNames=names)
    rows = [
        {
            "alarm": a["AlarmName"],
            "state": a["StateValue"],
            "reason": a.get("StateReason", ""),
            "updated": a.get("StateUpdatedTimestamp"),
        }
        for a in resp.get("MetricAlarms", [])
    ]
    return pd.DataFrame(rows)
