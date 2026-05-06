"""Synthetic `customers` row generator.

Two populations per run:
- ~95% net-new customers (Faker-generated)
- ~5% returning customers (sampled from the latest prior `customers`
  partition, with realistic mutations: address may change, income drifts,
  KYC may have expired, employment may have shifted).

The split + mutations are what give Phase 5 dbt's SCD2 staging something
non-trivial to work against. Without returning customers the source is
just a degenerate snapshot.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from faker import Faker

from lambdas.shared.faker_setup import make_faker

GENERATOR_VERSION = "customers/0.1.0"
SOURCE = "customers"

# Returning-customer share. Tuned to give Phase 5 SCD2 ~one change row per
# 20 customers per day — enough to exercise dbt without dominating volume.
RETURNING_SHARE_DEFAULT = 0.05

# KYC weights for new vs returning populations.
KYC_NEW = [("pending", 0.70), ("verified", 0.28), ("rejected", 0.01), ("expired", 0.01)]
KYC_RETURNING = [
    ("verified", 0.96),
    ("expired", 0.03),
    ("rejected", 0.01),
]
EMPLOYMENT = [
    ("employed", 0.65),
    ("self_employed", 0.15),
    ("unemployed", 0.10),
    ("retired", 0.08),
    ("student", 0.02),
]

# Returning-customer mutation rates.
ADDRESS_CHANGE_PROB = 0.10              # 10% moved
ADDRESS_OUT_OF_STATE_PROB = 0.50         # of moves, half cross state lines
INCOME_DRIFT_SIGMA = 0.05               # log-normal σ on returning income
INCOME_DRIFT_CLAMP = 0.25               # hard cap at ±25% per re-appearance
EMPLOYMENT_CHANGE_PROB = 0.08           # 8% transition
EMPLOYMENT_TO_UNEMPLOYED_PROB = 0.01    # of total returning, 1% become unemployed


def _weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    population = [opt[0] for opt in options]
    weights = [opt[1] for opt in options]
    return rng.choices(population, weights=weights, k=1)[0]


def _lognormal_decimal(
    rng: random.Random, *, mu: float, sigma: float, low: float, high: float
) -> Decimal:
    for _ in range(20):
        val = math.exp(rng.gauss(mu, sigma))
        if low <= val <= high:
            return Decimal(f"{val:.2f}")
    return Decimal(f"{max(low, min(high, val)):.2f}")


def _income_for_status(rng: random.Random, status: str) -> Decimal:
    if status == "unemployed":
        return _lognormal_decimal(rng, mu=8.5, sigma=0.4, low=0, high=20_000)
    if status == "retired":
        return _lognormal_decimal(rng, mu=10.2, sigma=0.4, low=15_000, high=120_000)
    if status == "self_employed":
        return _lognormal_decimal(rng, mu=11.0, sigma=0.7, low=20_000, high=400_000)
    if status == "student":
        return _lognormal_decimal(rng, mu=8.8, sigma=0.5, low=0, high=30_000)
    return _lognormal_decimal(rng, mu=10.9, sigma=0.5, low=20_000, high=300_000)


def _drift_income(rng: random.Random, current: Decimal) -> Decimal:
    raw_multiplier = math.exp(rng.gauss(0.0, INCOME_DRIFT_SIGMA))
    multiplier = max(1 - INCOME_DRIFT_CLAMP, min(1 + INCOME_DRIFT_CLAMP, raw_multiplier))
    new_val = float(current) * multiplier
    return Decimal(f"{max(0.0, new_val):.2f}")


def _new_customer_created_at(rng: random.Random, ingest_date: date) -> datetime:
    """New customers were created in the last 30 days, weighted toward recent."""
    end = datetime.combine(ingest_date, time(2, 50, 0), tzinfo=timezone.utc)
    days_ago = max(0.0, min(30.0, rng.expovariate(1 / 4.0)))
    return end - timedelta(days=days_ago)


def _new_customer_row(
    rng: random.Random, fake: Faker, ingest_date: date, ingest_at: datetime
) -> dict:
    employment = _weighted_choice(rng, EMPLOYMENT)
    income = _income_for_status(rng, employment)
    kyc = _weighted_choice(rng, KYC_NEW)
    created_at = _new_customer_created_at(rng, ingest_date)
    return {
        "customer_id": str(uuid4()),
        "first_name": fake.first_name(),
        "last_name": fake.last_name(),
        "email": fake.email(),
        "phone": fake.phone_number(),
        "date_of_birth": fake.date_of_birth(minimum_age=18, maximum_age=80),
        "address_line1": fake.street_address(),
        "city": fake.city(),
        "state": fake.state_abbr(),
        "zip": fake.zipcode(),
        "kyc_status": kyc,
        "employment_status": employment,
        "annual_income": income,
        "created_at": created_at,
        "updated_at": created_at,
        "is_returning": False,
        "_generator_version": GENERATOR_VERSION,
        "_ingest_at": ingest_at,
    }


def _returning_customer_row(
    rng: random.Random, fake: Faker, parent_row: dict, ingest_at: datetime
) -> dict:
    employment = parent_row["employment_status"]
    if rng.random() < EMPLOYMENT_CHANGE_PROB:
        if rng.random() < EMPLOYMENT_TO_UNEMPLOYED_PROB / EMPLOYMENT_CHANGE_PROB:
            employment = "unemployed"
        else:
            employment = _weighted_choice(rng, EMPLOYMENT)

    income = _drift_income(rng, parent_row["annual_income"])

    address_line1 = parent_row["address_line1"]
    city = parent_row["city"]
    state = parent_row["state"]
    zip_code = parent_row["zip"]
    if rng.random() < ADDRESS_CHANGE_PROB:
        address_line1 = fake.street_address()
        city = fake.city()
        zip_code = fake.zipcode()
        if rng.random() < ADDRESS_OUT_OF_STATE_PROB:
            state = fake.state_abbr()

    kyc = _weighted_choice(rng, KYC_RETURNING)

    return {
        "customer_id": parent_row["customer_id"],
        "first_name": parent_row["first_name"],
        "last_name": parent_row["last_name"],
        "email": parent_row["email"],
        "phone": parent_row["phone"],
        "date_of_birth": parent_row["date_of_birth"],
        "address_line1": address_line1,
        "city": city,
        "state": state,
        "zip": zip_code,
        "kyc_status": kyc,
        "employment_status": employment,
        "annual_income": income,
        "created_at": parent_row["created_at"],
        "updated_at": ingest_at,
        "is_returning": True,
        "_generator_version": GENERATOR_VERSION,
        "_ingest_at": ingest_at,
    }


def _coerce_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _coerce_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"Cannot coerce {type(value).__name__} to datetime")


def _normalise_parent_row(row: dict) -> dict:
    """Parent rows come from pyarrow → pylist; coerce decimals + timestamps."""
    return {
        **row,
        "annual_income": _coerce_decimal(row["annual_income"]),
        "created_at": _coerce_datetime(row["created_at"]),
    }


def make_rows(
    n: int,
    ingest_date: date,
    *,
    seed: int | None = None,
    ingest_at: datetime | None = None,
    parent_rows: Iterable[dict] | None = None,
    returning_share: float = RETURNING_SHARE_DEFAULT,
) -> list[dict]:
    if n < 0:
        raise ValueError("n must be >= 0")
    rng = random.Random(seed)
    fake = make_faker(seed=seed)
    ingest_at = ingest_at or datetime.now(timezone.utc)

    parents = list(parent_rows) if parent_rows is not None else []
    if not parents:
        # Phase 1 standalone or first-ever customer run — 100% net-new.
        return [_new_customer_row(rng, fake, ingest_date, ingest_at) for _ in range(n)]

    target_returning = round(n * returning_share)
    target_returning = min(target_returning, len(parents))
    target_new = n - target_returning

    sampled = rng.sample(parents, k=target_returning) if target_returning else []

    rows = [
        _returning_customer_row(rng, fake, _normalise_parent_row(p), ingest_at)
        for p in sampled
    ]
    rows.extend(_new_customer_row(rng, fake, ingest_date, ingest_at) for _ in range(target_new))
    rng.shuffle(rows)
    return rows
