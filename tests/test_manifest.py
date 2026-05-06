from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lambdas.shared.manifest import (
    build_manifest,
    schema_hash,
    utc_iso,
    write_manifest,
    write_success,
)
from lambdas.shared.parquet_writer import LOAN_APPLICATIONS_SCHEMA


def test_schema_hash_is_stable_and_starts_with_sha256():
    h1 = schema_hash(LOAN_APPLICATIONS_SCHEMA)
    h2 = schema_hash(LOAN_APPLICATIONS_SCHEMA)
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_utc_iso_naive_to_z():
    naive = datetime(2026, 5, 4, 3, 0, 0)
    s = utc_iso(naive)
    assert s.endswith("Z")


def test_build_manifest_shape():
    started = datetime(2026, 5, 4, 3, 0, 0, tzinfo=timezone.utc)
    finished = started + timedelta(seconds=5)
    m = build_manifest(
        run_id="abc",
        source="loan_applications",
        generator_version="loan_app/0.1.0",
        ingest_date="2026-05-04",
        parquet_key="raw/loan_applications/.../foo.parquet",
        rows=12000,
        bytes_=3145728,
        schema=LOAN_APPLICATIONS_SCHEMA,
        started_at=started,
        finished_at=finished,
        validation_passed=True,
    )
    assert m["rows"] == 12000
    assert m["duration_ms"] == 5000
    assert m["schema_hash"].startswith("sha256:")
    assert m["validation_errors"] == []
    assert m["validation_passed"] is True


def test_write_manifest_and_success(tmp_path):
    target = str(tmp_path / "m.json")
    n = write_manifest(target, {"hello": "world"})
    body = json.loads(Path(target).read_text())
    assert body == {"hello": "world"}
    assert n > 0

    success = str(tmp_path / "_SUCCESS")
    write_success(success)
    assert Path(success).exists()
    assert Path(success).read_bytes() == b""
