"""Local-FS tests for parent_partition.latest_success_partition.

S3 paths are exercised via integration tests in CI; the local branch
covers the bulk of the logic since both code paths are identical except
for the LIST mechanism.
"""

from __future__ import annotations

import random
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lambdas.shared.parent_partition import (
    latest_success_partition,
    list_parquet_uris,
    read_parent_columns,
    sample_fk_values,
)


def _write_partition(base, source: str, ingest_date: str, *, mark_success: bool = True):
    part = base / "raw" / source / f"ingest_date={ingest_date}"
    part.mkdir(parents=True, exist_ok=True)

    table = pa.table({"customer_id": [f"{ingest_date}-{i}" for i in range(5)]})
    pq.write_table(table, str(part / "data.parquet"))
    if mark_success:
        (part / "_SUCCESS").write_bytes(b"")
    return part


def test_returns_none_when_no_partition(tmp_path):
    assert latest_success_partition(str(tmp_path), "customers") is None


def test_picks_latest_success(tmp_path):
    _write_partition(tmp_path, "customers", "2026-05-01")
    _write_partition(tmp_path, "customers", "2026-05-03")
    _write_partition(tmp_path, "customers", "2026-05-02")

    latest = latest_success_partition(str(tmp_path), "customers")
    assert latest is not None
    assert latest.endswith("ingest_date=2026-05-03")


def test_skips_partitions_without_success_marker(tmp_path):
    _write_partition(tmp_path, "customers", "2026-05-01", mark_success=True)
    _write_partition(tmp_path, "customers", "2026-05-04", mark_success=False)

    latest = latest_success_partition(str(tmp_path), "customers")
    assert latest is not None
    assert latest.endswith("ingest_date=2026-05-01")


def test_before_filter_excludes_today(tmp_path):
    _write_partition(tmp_path, "customers", "2026-05-01")
    _write_partition(tmp_path, "customers", "2026-05-04")
    _write_partition(tmp_path, "customers", "2026-05-05")

    latest = latest_success_partition(
        str(tmp_path), "customers", before=date(2026, 5, 5)
    )
    assert latest is not None
    assert latest.endswith("ingest_date=2026-05-04")


def test_list_parquet_uris(tmp_path):
    part = _write_partition(tmp_path, "customers", "2026-05-01")
    parquet_files = list_parquet_uris(str(part))
    assert len(parquet_files) == 1
    assert parquet_files[0].endswith("data.parquet")


def test_read_parent_columns(tmp_path):
    part = _write_partition(tmp_path, "customers", "2026-05-01")
    cols = read_parent_columns(str(part), ["customer_id"])
    assert cols["customer_id"] == [f"2026-05-01-{i}" for i in range(5)]


def test_sample_fk_values_without_replacement(tmp_path):
    part = _write_partition(tmp_path, "customers", "2026-05-01")
    rng = random.Random(0)
    sample = sample_fk_values(str(part), id_column="customer_id", n=3, rng=rng)
    assert len(sample) == 3
    assert len(set(sample)) == 3


def test_sample_fk_values_with_replacement_when_n_exceeds_pool(tmp_path):
    part = _write_partition(tmp_path, "customers", "2026-05-01")
    rng = random.Random(0)
    sample = sample_fk_values(str(part), id_column="customer_id", n=20, rng=rng)
    assert len(sample) == 20


def test_sample_fk_values_raises_on_empty(tmp_path):
    part = tmp_path / "raw" / "customers" / "ingest_date=2026-05-01"
    part.mkdir(parents=True)
    table = pa.table({"customer_id": pa.array([], type=pa.string())})
    pq.write_table(table, str(part / "data.parquet"))
    with pytest.raises(ValueError):
        sample_fk_values(str(part), id_column="customer_id", n=1, rng=random.Random(0))
