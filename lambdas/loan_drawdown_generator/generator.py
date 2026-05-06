"""Synthetic loan_drawdowns row generator.

Filtered fan-out from loan_decisions: only rows where decision='approved'
produce a drawdown. ~75% of decisions in steady state. Per
docs/02-fan-out.md §13.4:

- 70% of approved customers draw the **full** approved amount
- 30% draw partial: uniform between 30% and 99%
- account_last4 = last 4 digits of a fake 16-digit number
- disbursed_at = decided_at + log-normal delay (skewed early), capped 48h

We denormalize approved_amount, apr_pct, term_months onto the drawdown
row because payments / delinquencies need them and we don't want every
downstream generator to re-join all the way back to decisions.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

GENERATOR_VERSION = "drawdowns/0.1.0"
SOURCE = "loan_drawdowns"

FULL_DRAW_PROB = 0.70
PARTIAL_MIN_FRAC = 0.30
PARTIAL_MAX_FRAC = 0.99
DRAW_DELAY_HOURS_MAX = 48.0


def _coerce_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"Cannot coerce {type(value).__name__} to datetime")


def _coerce_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _drawn_amount(rng: random.Random, approved: Decimal) -> Decimal:
    if rng.random() < FULL_DRAW_PROB:
        return approved
    fraction = rng.uniform(PARTIAL_MIN_FRAC, PARTIAL_MAX_FRAC)
    return Decimal(f"{float(approved) * fraction:.2f}")


def _account_last4(rng: random.Random) -> str:
    return f"{rng.randint(0, 9999):04d}"


def _disbursed_at(rng: random.Random, decided_at: datetime) -> datetime:
    """Log-normal delay skewed early. mu=1.5, sigma=1.0 → median ~4.5h, tail to 48h."""
    for _ in range(20):
        hours = math.exp(rng.gauss(1.5, 1.0))
        if hours <= DRAW_DELAY_HOURS_MAX:
            return decided_at + timedelta(hours=hours)
    return decided_at + timedelta(hours=DRAW_DELAY_HOURS_MAX)


def _row_for_decision(
    rng: random.Random, decision: dict, ingest_at: datetime
) -> dict:
    approved = _coerce_decimal(decision["approved_amount"])
    drawn = _drawn_amount(rng, approved)
    return {
        "drawdown_id": str(uuid4()),
        "decision_id": decision["decision_id"],
        "application_id": decision["application_id"],
        "customer_id": decision["customer_id"],
        "drawn_amount": drawn,
        "approved_amount": approved,
        "apr_pct": _coerce_decimal(decision["apr_pct"]),
        "term_months": int(decision["term_months"]),
        "account_last4": _account_last4(rng),
        "disbursed_at": _disbursed_at(rng, _coerce_datetime(decision["decided_at"])),
        "_generator_version": GENERATOR_VERSION,
        "_ingest_at": ingest_at,
    }


def make_rows(
    approved_decisions: Iterable[dict],
    *,
    seed: int | None = None,
    ingest_at: datetime | None = None,
) -> list[dict]:
    """Each input must be a decision row where decision='approved' (the
    handler is responsible for filtering)."""
    rows = list(approved_decisions)
    if not rows:
        raise ValueError("loan_drawdowns requires at least one approved decision")
    rng = random.Random(seed)
    ingest_at = ingest_at or datetime.now(timezone.utc)
    return [_row_for_decision(rng, d, ingest_at) for d in rows]
