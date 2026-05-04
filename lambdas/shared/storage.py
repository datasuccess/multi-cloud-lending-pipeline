"""URI dispatch: write/read bytes to either S3 or a local path.

Local paths let unit tests run with no AWS dependency. S3 paths take the same
shape (`s3://bucket/key`) used by the Lambda in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import boto3


@dataclass(frozen=True)
class ParsedUri:
    scheme: str
    bucket: str | None
    key: str

    @property
    def is_s3(self) -> bool:
        return self.scheme == "s3"


def parse_uri(uri: str) -> ParsedUri:
    if uri.startswith("s3://"):
        without_scheme = uri[len("s3://") :]
        bucket, _, key = without_scheme.partition("/")
        if not bucket or not key:
            raise ValueError(f"Malformed S3 URI: {uri}")
        return ParsedUri(scheme="s3", bucket=bucket, key=key)
    return ParsedUri(scheme="file", bucket=None, key=uri)


def _s3_client():
    return boto3.client("s3")


def write_bytes(uri: str, body: bytes, *, content_type: str | None = None) -> None:
    parsed = parse_uri(uri)
    if parsed.is_s3:
        kwargs = {"Bucket": parsed.bucket, "Key": parsed.key, "Body": body}
        if content_type:
            kwargs["ContentType"] = content_type
        _s3_client().put_object(**kwargs)
        return

    path = Path(parsed.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def read_bytes(uri: str) -> bytes:
    parsed = parse_uri(uri)
    if parsed.is_s3:
        response = _s3_client().get_object(Bucket=parsed.bucket, Key=parsed.key)
        return response["Body"].read()
    return Path(parsed.key).read_bytes()


def append_line(uri: str, line: str) -> None:
    """Append one JSONL record. Local: real append. S3: read-modify-write.

    Phase 1 has at most one Lambda invocation per partition per day, so the
    read-modify-write race window is academic. Phase 2 moves the ledger to a
    one-object-per-run layout (already reflected in the path scheme), which
    sidesteps this entirely.
    """
    parsed = parse_uri(uri)
    if parsed.is_s3:
        existing = b""
        client = _s3_client()
        try:
            existing = client.get_object(Bucket=parsed.bucket, Key=parsed.key)["Body"].read()
        except client.exceptions.NoSuchKey:
            pass
        body = existing + (line + "\n").encode("utf-8")
        client.put_object(Bucket=parsed.bucket, Key=parsed.key, Body=body)
        return

    path = Path(parsed.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def join(*parts: str) -> str:
    """Join URI parts. Works for both s3:// and local paths."""
    if not parts:
        return ""
    head, *tail = parts
    head = head.rstrip("/")
    rest = "/".join(p.strip("/") for p in tail if p)
    if rest:
        return f"{head}/{rest}"
    return head


def env_uri(var: str, default: str | None = None) -> str:
    """Read a URI from an env var. Used so tests can point at tmp_path."""
    val = os.environ.get(var, default)
    if val is None:
        raise RuntimeError(f"Missing env var: {var}")
    return val
