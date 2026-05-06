"""Synthetic loan_decisions row generator with a real rule engine.

Inputs (joined upstream):
- loan_applications: application_id, customer_id, amount_requested,
  annual_income, term_months
- credit_bureau_pulls: application_id, bureau_score, pulled_at

Decision logic (docs/02-fan-out.md §13.3):

1. Hard rule: requested / annual_income > 0.5 ⇒ declined, high_dti.
2. Score band determines approve rate:
     <580:    5%  approve
     580-669: 60% approve
     670-739: 90% approve
     740+:    98% approve
3. ~3% of approved decisions become 'referred' (manual review queue).
4. Decline reason is sampled from the band's typical mix.
5. APR conditional on score band:
     740+:    6-10%
     670-739: 10-15%
     580-669: 15-24%
     <580 (rare approval): 24-30%
6. Approved amount = requested, except for 'capacity_exceeded' reason
   on prime borrowers — 50-90% of requested.
7. decided_at = pulled_at + uniform(1m, 10m).

For declined / referred decisions, apr_pct and approved_amount are null.
term_months is always present (declined decisions still record what
was requested).
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

GENERATOR_VERSION = "decisions/0.1.0"
SOURCE = "loan_decisions"

REFERRAL_RATE = 0.03                # share of approved that become 'referred'
DTI_HARD_LIMIT = 0.5                # requested / income above this auto-declines
DECISION_DELAY_MIN_SEC = 60
DECISION_DELAY_MAX_SEC = 10 * 60


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


def _score_band(score: int) -> str:
    if score < 580:
        return "subprime"
    if score < 670:
        return "near_prime"
    if score < 740:
        return "prime"
    return "super_prime"


APPROVE_RATE = {
    "subprime": 0.05,
    "near_prime": 0.60,
    "prime": 0.90,
    "super_prime": 0.98,
}

# Decline-reason weights per band. low_score dominates at the bottom,
# capacity / manual at the top.
DECLINE_REASONS = {
    "subprime": [("low_score", 0.95), ("high_dti", 0.04), ("fraud_flag", 0.01)],
    "near_prime": [
        ("low_score", 0.55),
        ("high_dti", 0.35),
        ("income_unverified", 0.08),
        ("fraud_flag", 0.02),
    ],
    "prime": [
        ("high_dti", 0.55),
        ("income_unverified", 0.35),
        ("low_score", 0.05),
        ("fraud_flag", 0.05),
    ],
    "super_prime": [
        ("capacity_exceeded", 0.60),
        ("manual_referral", 0.30),
        ("income_unverified", 0.10),
    ],
}

APR_BAND = {
    "subprime": (Decimal("24.00"), Decimal("30.00")),
    "near_prime": (Decimal("15.00"), Decimal("24.00")),
    "prime": (Decimal("10.00"), Decimal("15.00")),
    "super_prime": (Decimal("6.00"), Decimal("10.00")),
}


def _weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    population = [opt[0] for opt in options]
    weights = [opt[1] for opt in options]
    return rng.choices(population, weights=weights, k=1)[0]


def _apr_for_band(rng: random.Random, band: str) -> Decimal:
    low, high = APR_BAND[band]
    raw = float(low) + rng.random() * (float(high) - float(low))
    return Decimal(f"{raw:.2f}")


def _approved_amount(
    rng: random.Random, requested: Decimal, reason: str
) -> Decimal:
    if reason == "capacity_exceeded":
        return Decimal(f"{float(requested) * (0.5 + rng.random() * 0.4):.2f}")
    return requested


def _decided_at(rng: random.Random, pulled_at: datetime) -> datetime:
    delay = rng.uniform(DECISION_DELAY_MIN_SEC, DECISION_DELAY_MAX_SEC)
    return pulled_at + timedelta(seconds=delay)


def _row_for_application(
    rng: random.Random, joined: dict, ingest_at: datetime
) -> dict:
    requested = _coerce_decimal(joined["amount_requested"])
    income = _coerce_decimal(joined["annual_income"])
    score = int(joined["bureau_score"])
    band = _score_band(score)
    pulled_at = _coerce_datetime(joined["pulled_at"])
    decided_at = _decided_at(rng, pulled_at)

    # Hard DTI rule overrides everything except score-floor.
    dti = float(requested) / float(income) if float(income) > 0 else 99.0
    if dti > DTI_HARD_LIMIT:
        return {
            "decision_id": str(uuid4()),
            "application_id": joined["application_id"],
            "customer_id": joined["customer_id"],
            "decision": "declined",
            "decision_reason": "high_dti",
            "apr_pct": None,
            "approved_amount": None,
            "term_months": int(joined["term_months"]),
            "decided_at": decided_at,
            "_generator_version": GENERATOR_VERSION,
            "_ingest_at": ingest_at,
        }

    if rng.random() < APPROVE_RATE[band]:
        if rng.random() < REFERRAL_RATE:
            decision = "referred"
            reason = "manual_referral"
            apr = None
            approved = None
        else:
            decision = "approved"
            reason = "clean"
            # Super-prime sometimes hits capacity even when approved.
            if band == "super_prime" and rng.random() < 0.04:
                reason = "capacity_exceeded"
                approved = _approved_amount(rng, requested, reason)
            else:
                approved = requested
            apr = _apr_for_band(rng, band)
    else:
        decision = "declined"
        reason = _weighted_choice(rng, DECLINE_REASONS[band])
        apr = None
        approved = None

    return {
        "decision_id": str(uuid4()),
        "application_id": joined["application_id"],
        "customer_id": joined["customer_id"],
        "decision": decision,
        "decision_reason": reason,
        "apr_pct": apr,
        "approved_amount": approved,
        "term_months": int(joined["term_months"]),
        "decided_at": decided_at,
        "_generator_version": GENERATOR_VERSION,
        "_ingest_at": ingest_at,
    }


def make_rows(
    joined_applications: Iterable[dict],
    *,
    seed: int | None = None,
    ingest_at: datetime | None = None,
) -> list[dict]:
    """Each input row must already be the join of loan_applications and
    credit_bureau_pulls — see handler._join_apps_and_bureau."""
    rows = list(joined_applications)
    if not rows:
        raise ValueError("loan_decisions requires at least one parent application")
    rng = random.Random(seed)
    ingest_at = ingest_at or datetime.now(timezone.utc)
    return [_row_for_application(rng, r, ingest_at) for r in rows]
