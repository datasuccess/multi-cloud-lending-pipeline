"""Tests for the Secrets Manager helper.

Local-stub mode is exercised on disk; the AWS path is mocked at the boto3
boundary so no network calls happen.
"""

from __future__ import annotations

import json

import pytest

from lambdas.shared import secrets as s


@pytest.fixture(autouse=True)
def _reset_caches():
    s.clear_cache()
    yield
    s.clear_cache()


def test_local_stub_loads_from_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LENDING_SECRETS_LOCAL", "1")
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "lending_dev_streamlit-config.json").write_text(
        json.dumps({"raw_bucket": "lending-raw-1", "region": "us-east-1"})
    )

    out = s.get_secret("lending/dev/streamlit-config")

    assert out == {"raw_bucket": "lending-raw-1", "region": "us-east-1"}


def test_local_stub_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LENDING_SECRETS_LOCAL", "1")

    with pytest.raises(FileNotFoundError):
        s.get_secret("lending/dev/missing")


def test_aws_path_calls_secretsmanager(monkeypatch):
    """When LENDING_SECRETS_LOCAL is unset, we hit boto3."""
    monkeypatch.delenv("LENDING_SECRETS_LOCAL", raising=False)

    calls: list[str] = []

    class FakeClient:
        def get_secret_value(self, SecretId: str) -> dict[str, str]:
            calls.append(SecretId)
            return {"SecretString": json.dumps({"k": "v"})}

    monkeypatch.setattr(s, "_client", lambda: FakeClient())

    out = s.get_secret("lending/dev/foo")

    assert out == {"k": "v"}
    assert calls == ["lending/dev/foo"]


def test_get_secret_is_cached(monkeypatch):
    monkeypatch.delenv("LENDING_SECRETS_LOCAL", raising=False)

    calls: list[str] = []

    class FakeClient:
        def get_secret_value(self, SecretId: str) -> dict[str, str]:
            calls.append(SecretId)
            return {"SecretString": json.dumps({"k": "v"})}

    monkeypatch.setattr(s, "_client", lambda: FakeClient())

    s.get_secret("lending/dev/foo")
    s.get_secret("lending/dev/foo")
    s.get_secret("lending/dev/foo")

    assert calls == ["lending/dev/foo"]  # only one round-trip
