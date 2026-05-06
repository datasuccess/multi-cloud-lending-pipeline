from __future__ import annotations

import pytest

from lambdas.shared.storage import append_line, join, parse_uri, read_bytes, write_bytes


def test_parse_uri_s3():
    p = parse_uri("s3://my-bucket/some/key.parquet")
    assert p.is_s3
    assert p.bucket == "my-bucket"
    assert p.key == "some/key.parquet"


def test_parse_uri_local():
    p = parse_uri("/tmp/foo/bar")
    assert not p.is_s3
    assert p.bucket is None
    assert p.key == "/tmp/foo/bar"


def test_parse_uri_rejects_malformed_s3():
    with pytest.raises(ValueError):
        parse_uri("s3://only-bucket")


def test_join():
    assert join("/tmp/x", "y", "z") == "/tmp/x/y/z"
    assert join("s3://b", "raw", "table", "ingest_date=2026-01-01") == (
        "s3://b/raw/table/ingest_date=2026-01-01"
    )
    assert join("s3://b/", "/raw/", "/x") == "s3://b/raw/x"


def test_local_round_trip(tmp_path):
    target = str(tmp_path / "nested" / "blob.bin")
    write_bytes(target, b"hello")
    assert read_bytes(target) == b"hello"


def test_append_line_local(tmp_path):
    target = str(tmp_path / "ledger.jsonl")
    append_line(target, '{"a":1}')
    append_line(target, '{"a":2}')
    body = (tmp_path / "ledger.jsonl").read_text()
    assert body == '{"a":1}\n{"a":2}\n'
