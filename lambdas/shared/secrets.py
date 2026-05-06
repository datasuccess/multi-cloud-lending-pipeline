"""Thin wrapper over AWS Secrets Manager.

Conventions (mirrored across the /practice projects — see
`practice/INFRASTRUCTURE_NOTES.md` §4):

  - Secret names are namespaced `lending/<env>/<resource>` (e.g.
    `lending/dev/streamlit-config`).
  - Stored payload is always a JSON object; `get_secret` returns a `dict`.
  - Results are cached per-name with `lru_cache` because Lambdas / Streamlit
    re-resolve the same secret many times per process and the call is paid.
  - Boto3 client is also lazy + cached so importing this module is free in
    tests where no AWS calls happen.

Local dev: set `LENDING_SECRETS_LOCAL=1` and put a JSON file at
`./.secrets/<name>.json` (slash → underscore in the file name) — `get_secret`
reads from disk instead of AWS. Keeps the Streamlit dev loop offline.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def _client() -> Any:
    import boto3

    return boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))


@lru_cache(maxsize=32)
def get_secret(name: str) -> dict[str, Any]:
    """Resolve a JSON secret by name; result is cached for the process lifetime."""
    if os.environ.get("LENDING_SECRETS_LOCAL") == "1":
        return _load_local(name)

    resp = _client().get_secret_value(SecretId=name)
    raw = resp.get("SecretString")
    if raw is None:
        # Binary secrets aren't used in this project.
        raise ValueError(f"secret {name!r} has no SecretString")
    return json.loads(raw)


def _load_local(name: str) -> dict[str, Any]:
    fname = name.replace("/", "_") + ".json"
    path = Path(".secrets") / fname
    if not path.exists():
        raise FileNotFoundError(
            f"local secret stub not found: {path} (set LENDING_SECRETS_LOCAL=0 to hit AWS)"
        )
    return json.loads(path.read_text())


def clear_cache() -> None:
    """For tests — drop both the boto3 client cache and resolved values."""
    _client.cache_clear()
    get_secret.cache_clear()
