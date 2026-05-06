"""Synthetic credit_bureau_pulls row generator.

One pull per loan_application — exhaustive iteration over the parent
partition, not random sampling. Realism rules (docs/02-fan-out.md §13):

- `pulled_at` is 1–30 minutes after the application's `applied_at`.
- Bureau score distribution targets a US FICO shape — median ~700,
  fat tails. Implemented as Beta(8, 4) scaled to [300, 850].
- `bureau_name` roughly equal across the three bureaus (real lenders
  usually pull from one, but for synthetic data we want each bureau
  represented).
- `hard_inquiry` is ~95% true (loan apps almost always trigger one).
- `tradelines_count` ≈ Poisson(8), clipped 0–30.
- `delinquencies_count` correlates inversely with score: prime borrowers
  have ~0 delinquencies, sub-580 borrowers see Poisson(2).

A pull always exists for every application — fully empty parents raise.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

GENERATOR_VERSION = "bureau/0.1.0"
SOURCE = "credit_bureau_pulls"

BUREAUS = ["experian", "equifax", "transunion"]
HARD_INQUIRY_PROB = 0.95
PULL_DELAY_MIN_SEC = 60
PULL_DELAY_MAX_SEC = 30 * 60


def _coerce_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"Cannot coerce {type(value).__name__} to datetime")


def _sample_score(rng: random.Random) -> int:
    """Beta(8, 4) scaled to [300, 850] gives a FICO-ish shape with median ~700."""
    raw = rng.betavariate(8.0, 4.0)
    return int(round(300 + raw * (850 - 300)))


def _sample_tradelines(rng: random.Random) -> int:
    """Poisson(8), clipped 0..30. Manual sampler — Python stdlib lacks Poisson."""
    L = math.exp(-8.0)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return min(k - 1, 30)


def _sample_delinquencies(rng: random.Random, score: int) -> int:
    """Mean inversely scales with score. Sub-580 → Poisson(2). 740+ → Poisson(0.05)."""
    if score < 580:
        mean = 2.0
    elif score < 670:
        mean = 1.0
    elif score < 740:
        mean = 0.3
    else:
        mean = 0.05
    L = math.exp(-mean)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return min(k - 1, 10)


def _pulled_at(rng: random.Random, applied_at: datetime) -> datetime:
    delay_seconds = rng.uniform(PULL_DELAY_MIN_SEC, PULL_DELAY_MAX_SEC)
    return applied_at + timedelta(seconds=delay_seconds)


def _row_for_application(rng: random.Random, app: dict, ingest_at: datetime) -> dict:
    score = _sample_score(rng)
    return {
        "pull_id": str(uuid4()),
        "application_id": app["application_id"],
        "customer_id": app["customer_id"],
        "bureau_name": rng.choice(BUREAUS),
        "bureau_score": score,
        "hard_inquiry": rng.random() < HARD_INQUIRY_PROB,
        "tradelines_count": _sample_tradelines(rng),
        "delinquencies_count": _sample_delinquencies(rng, score),
        "pulled_at": _pulled_at(rng, _coerce_datetime(app["applied_at"])),
        "_generator_version": GENERATOR_VERSION,
        "_ingest_at": ingest_at,
    }


def make_rows(
    parent_applications: Iterable[dict],
    *,
    seed: int | None = None,
    ingest_at: datetime | None = None,
) -> list[dict]:
    apps = list(parent_applications)
    if not apps:
        raise ValueError("credit_bureau_pulls requires at least one parent application")
    rng = random.Random(seed)
    ingest_at = ingest_at or datetime.now(timezone.utc)
    return [_row_for_application(rng, app, ingest_at) for app in apps]
