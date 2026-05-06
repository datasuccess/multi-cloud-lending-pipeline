"""Synthetic `payments` row generator with a state-dependent (Markov-ish)
status transition matrix.

Inputs:
- drawdowns: drawdown_id, customer_id, drawn_amount, apr_pct, term_months
- prior payments (optional): drawdown_id → prior payment_status, used to
  drive the Markov transition. First-ever run has no prior state, so
  every drawdown starts from the favorable "paid_full" state.

Per docs/02-fan-out.md §13.5:

| Prior status      | paid_full | paid_partial | missed |
|-------------------|-----------|--------------|--------|
| paid_full / new   | 92%       | 5%           | 3%     |
| paid_partial      | 60%       | 25%          | 15%    |
| missed            | 35%       | 25%          | 40%    |

Once you miss, you're more likely to miss again — that cascade is what
makes Phase 5's delinquency dbt staging non-trivial.

Amortization is deliberately simplified (constant interest share per
period derived from APR/12). Phase 5 dbt is not asked to validate
financial mathematics — only data shape and FK integrity.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

GENERATOR_VERSION = "payments/0.1.0"
SOURCE = "payments"

PAYMENTS_CAP = 30_000

TRANSITIONS = {
    "paid_full": [("paid_full", 0.92), ("paid_partial", 0.05), ("missed", 0.03)],
    "paid_partial": [("paid_full", 0.60), ("paid_partial", 0.25), ("missed", 0.15)],
    "missed": [("paid_full", 0.35), ("paid_partial", 0.25), ("missed", 0.40)],
}
WAIVED_RATE = 0.001  # tiny share of payments get waived (lender ops decision)


def _coerce_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    population = [opt[0] for opt in options]
    weights = [opt[1] for opt in options]
    return rng.choices(population, weights=weights, k=1)[0]


def _scheduled_amount(
    principal: Decimal, apr_pct: Decimal, term_months: int
) -> Decimal:
    """Standard fixed-payment amortization: M = P * r / (1 - (1+r)^-n).

    Term must be > 0. APR is annual; r is monthly.
    """
    r = float(apr_pct) / 12.0 / 100.0
    p = float(principal)
    n = max(int(term_months), 1)
    if r == 0:
        m = p / n
    else:
        m = p * r / (1.0 - (1.0 + r) ** (-n))
    return Decimal(f"{m:.2f}")


def _interest_share(scheduled: Decimal, principal: Decimal, apr_pct: Decimal) -> Decimal:
    """Approximate interest portion of one payment: APR/12 * remaining principal.

    We don't track remaining principal across runs — simplification: take
    the *first-period* interest share, which is `principal * APR/12`. This
    is an overstatement for late-life payments but keeps the math
    self-contained for Phase 2 synthetic data.
    """
    monthly_rate = float(apr_pct) / 12.0 / 100.0
    interest = float(principal) * monthly_rate
    interest = min(interest, float(scheduled))  # never exceed scheduled
    return Decimal(f"{interest:.2f}")


def _paid_at(
    rng: random.Random, scheduled_at: date, ingest_at: datetime
) -> datetime:
    """Roughly within ±5 days of scheduled_at; tz-aware."""
    delta_days = rng.uniform(-3.0, 5.0)
    base = datetime.combine(scheduled_at, datetime.min.time(), tzinfo=timezone.utc)
    return base + timedelta(days=delta_days, hours=rng.uniform(0, 23))


def _row_for_drawdown(
    rng: random.Random,
    drawdown: dict,
    *,
    prior_status: str | None,
    scheduled_at: date,
    ingest_at: datetime,
) -> dict:
    principal_outstanding = _coerce_decimal(drawdown["drawn_amount"])
    apr = _coerce_decimal(drawdown["apr_pct"])
    term = int(drawdown["term_months"])
    scheduled = _scheduled_amount(principal_outstanding, apr, term)

    # Tiny chance of waiver, regardless of prior state.
    if rng.random() < WAIVED_RATE:
        status = "waived"
    else:
        prior = prior_status if prior_status in TRANSITIONS else "paid_full"
        status = _weighted_choice(rng, TRANSITIONS[prior])

    if status == "paid_full":
        actual = scheduled
        interest = _interest_share(scheduled, principal_outstanding, apr)
        principal = Decimal(f"{float(actual) - float(interest):.2f}")
        paid_at = _paid_at(rng, scheduled_at, ingest_at)
    elif status == "paid_partial":
        fraction = rng.uniform(0.30, 0.95)
        actual = Decimal(f"{float(scheduled) * fraction:.2f}")
        interest = _interest_share(scheduled, principal_outstanding, apr)
        # Interest is paid first; principal absorbs whatever's left.
        if actual <= interest:
            interest = actual
            principal = Decimal("0.00")
        else:
            principal = Decimal(f"{float(actual) - float(interest):.2f}")
        paid_at = _paid_at(rng, scheduled_at, ingest_at)
    else:
        # missed or waived — nothing flows.
        actual = Decimal("0.00")
        interest = Decimal("0.00")
        principal = Decimal("0.00")
        paid_at = None

    return {
        "payment_id": str(uuid4()),
        "drawdown_id": drawdown["drawdown_id"],
        "customer_id": drawdown["customer_id"],
        "scheduled_amount": scheduled,
        "actual_amount": actual,
        "principal_amount": principal,
        "interest_amount": interest,
        "payment_status": status,
        "scheduled_at": scheduled_at,
        "paid_at": paid_at,
        "_generator_version": GENERATOR_VERSION,
        "_ingest_at": ingest_at,
    }


def make_rows(
    drawdowns: Iterable[dict],
    *,
    seed: int | None = None,
    ingest_at: datetime | None = None,
    scheduled_at: date | None = None,
    prior_status_by_drawdown: dict[str, str] | None = None,
    cap: int = PAYMENTS_CAP,
) -> list[dict]:
    drawdowns_list = list(drawdowns)
    if not drawdowns_list:
        raise ValueError("payments requires at least one parent drawdown")
    if cap > 0 and len(drawdowns_list) > cap:
        # Deterministic truncation by drawdown_id keeps Markov continuity
        # across runs even after the cap kicks in.
        drawdowns_list = sorted(drawdowns_list, key=lambda d: d["drawdown_id"])[:cap]

    rng = random.Random(seed)
    ingest_at = ingest_at or datetime.now(timezone.utc)
    scheduled_at = scheduled_at or ingest_at.date()
    prior = prior_status_by_drawdown or {}

    return [
        _row_for_drawdown(
            rng,
            d,
            prior_status=prior.get(d["drawdown_id"]),
            scheduled_at=scheduled_at,
            ingest_at=ingest_at,
        )
        for d in drawdowns_list
    ]
