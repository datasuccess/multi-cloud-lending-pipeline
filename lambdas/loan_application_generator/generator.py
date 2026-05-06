"""Synthetic loan_applications row generator.

Distributions are deliberately not uniform:
- amount_requested: log-normal in [1k, 50k]
- annual_income: log-normal in [12k, 400k], conditioned on employment_status
- existing_debt: ~uniform-on-low end, capped at 5x annual_income
- applied_at: business-hours-skewed across the 24h preceding the run
- channel/purpose/employment_status/gov_id_type: weighted enums
- country: 90% US, ~10% other

FK contract (Phase 2):
- When a `customers` parent partition exists, customer_id and the
  denormalized identity fields (name, email, phone, dob, address) are
  sampled from there with replacement (customers can apply multiple
  times in a day). Loan-app-only fields (ssn, gov_id, ip, user_agent,
  loan terms) are still generated fresh.
- When no parent partition exists (Phase 1 standalone, or first-ever
  Phase 2 run before customers landed), customer_id is a fresh uuid4
  and the row is fully Faker-generated. The handler logs WARN.
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

GENERATOR_VERSION = "loan_app/0.1.0"
SOURCE = "loan_applications"

CHANNELS = [("web", 0.55), ("mobile", 0.30), ("branch", 0.10), ("partner", 0.05)]
PURPOSES = [
    ("debt_consolidation", 0.40),
    ("home_improvement", 0.20),
    ("auto", 0.15),
    ("medical", 0.10),
    ("other", 0.15),
]
EMPLOYMENT = [
    ("employed", 0.65),
    ("self_employed", 0.15),
    ("unemployed", 0.10),
    ("retired", 0.10),
]
GOV_ID_TYPES = [("drivers_license", 0.70), ("passport", 0.15), ("state_id", 0.15)]
TERMS = [12, 24, 36, 48, 60]

# Hour-of-day weights (UTC roughly == US daytime offset by ~5-8h, kept simple).
# Peak 14-22 UTC ≈ 9am-5pm Eastern.
HOUR_WEIGHTS = [
    1, 1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 7,
    8, 9, 10, 11, 11, 10, 8, 6, 4, 3, 2, 1,
]


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
    return _lognormal_decimal(rng, mu=10.9, sigma=0.5, low=20_000, high=300_000)


def _existing_debt(rng: random.Random, income: Decimal) -> Decimal:
    ratio = rng.betavariate(1.5, 4.0)
    debt = float(income) * ratio * 5
    return Decimal(f"{max(0.0, min(debt, float(income) * 5)):.2f}")


def _gov_id_number(fake: Faker, gov_id_type: str) -> str:
    if gov_id_type == "drivers_license":
        return fake.bothify(text="?#######").upper()
    if gov_id_type == "passport":
        return fake.bothify(text="?########").upper()
    return fake.bothify(text="??######").upper()


def _country(rng: random.Random) -> str:
    return "US" if rng.random() < 0.9 else rng.choice(["CA", "GB", "MX", "DE", "AU"])


def _hour(rng: random.Random) -> int:
    return rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]


def _applied_at_for_run(
    rng: random.Random, ingest_date: date
) -> datetime:
    """Pick a timestamp in the 24h preceding 03:00 UTC on ingest_date."""
    end = datetime.combine(ingest_date, time(3, 0, 0), tzinfo=timezone.utc)
    delta_hours = _hour(rng) + rng.random()
    return end - timedelta(hours=delta_hours)


def _date_of_birth(rng: random.Random, fake: Faker) -> date:
    return fake.date_of_birth(minimum_age=18, maximum_age=80)


def _coerce_dob(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"Cannot coerce {type(value).__name__} to date")


def _row_common(
    rng: random.Random, fake: Faker, ingest_date: date, ingest_at: datetime
) -> dict:
    """Loan-app-only fields that don't depend on the parent customer."""
    employment = _weighted_choice(rng, EMPLOYMENT)
    gov_id_type = _weighted_choice(rng, GOV_ID_TYPES)
    income = _income_for_status(rng, employment)
    debt = _existing_debt(rng, income)
    purpose = _weighted_choice(rng, PURPOSES)
    declared = fake.sentence(nb_words=12) if rng.random() < 0.5 else None
    referrer = (
        rng.choice(["google", "facebook", "instagram", "email", "affiliate"])
        if rng.random() < 0.7
        else None
    )
    return {
        "application_id": str(uuid4()),
        "applied_at": _applied_at_for_run(rng, ingest_date),
        "ssn": fake.ssn(),
        "gov_id_type": gov_id_type,
        "gov_id_number": _gov_id_number(fake, gov_id_type),
        "country": _country(rng),
        "ip_address": fake.ipv4(),
        "user_agent": fake.user_agent(),
        "amount_requested": _lognormal_decimal(
            rng, mu=8.8, sigma=0.6, low=1_000, high=50_000
        ),
        "term_months": rng.choice(TERMS),
        "purpose": purpose,
        "channel": _weighted_choice(rng, CHANNELS),
        "employment_status": employment,
        "annual_income": income,
        "existing_debt": debt,
        "referrer_source": referrer,
        "declared_purpose_text": declared,
        "status": "submitted",
        "_generator_version": GENERATOR_VERSION,
        "_ingest_at": ingest_at,
    }


def _row_from_faker(
    rng: random.Random, fake: Faker, ingest_date: date, ingest_at: datetime
) -> dict:
    """No-parent path — fully Faker-generated, unique customer_id per row."""
    common = _row_common(rng, fake, ingest_date, ingest_at)
    common.update(
        {
            "customer_id": str(uuid4()),
            "first_name": fake.first_name(),
            "last_name": fake.last_name(),
            "email": fake.email(),
            "phone": fake.phone_number(),
            "date_of_birth": _date_of_birth(rng, fake),
            "street_address": fake.street_address(),
            "city": fake.city(),
            "state": fake.state_abbr(),
            "zip": fake.zipcode(),
        }
    )
    return common


def _row_from_parent(
    rng: random.Random,
    fake: Faker,
    parent: dict,
    ingest_date: date,
    ingest_at: datetime,
) -> dict:
    """Parent-path — denormalize identity fields from the customers row."""
    common = _row_common(rng, fake, ingest_date, ingest_at)
    common.update(
        {
            "customer_id": parent["customer_id"],
            "first_name": parent["first_name"],
            "last_name": parent["last_name"],
            "email": parent["email"],
            "phone": parent["phone"],
            "date_of_birth": _coerce_dob(parent["date_of_birth"]),
            "street_address": parent["address_line1"],
            "city": parent["city"],
            "state": parent["state"],
            "zip": parent["zip"],
        }
    )
    return common


def make_rows(
    n: int,
    ingest_date: date,
    *,
    seed: int | None = None,
    ingest_at: datetime | None = None,
    parent_customer_rows: Iterable[dict] | None = None,
) -> list[dict]:
    if n < 0:
        raise ValueError("n must be >= 0")
    rng = random.Random(seed)
    fake = make_faker(seed=seed)
    ingest_at = ingest_at or datetime.now(timezone.utc)

    parents = list(parent_customer_rows) if parent_customer_rows is not None else []
    if not parents:
        return [_row_from_faker(rng, fake, ingest_date, ingest_at) for _ in range(n)]

    # Sample with replacement: customers can submit multiple applications in
    # one day, and loan_apps target volume (~14k) > customers (~12k).
    sampled = rng.choices(parents, k=n) if n > len(parents) else rng.sample(parents, k=n)
    return [_row_from_parent(rng, fake, p, ingest_date, ingest_at) for p in sampled]
