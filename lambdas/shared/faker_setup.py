"""Seeded en_US Faker bootstrap.

Centralised so every generator gets identical providers/locale and so a single
seed produces a deterministic dataset for replay tests and chaos drills.
"""

from __future__ import annotations

import random

from faker import Faker

DEFAULT_LOCALE = "en_US"


def make_faker(seed: int | None = None, locale: str = DEFAULT_LOCALE) -> Faker:
    fake = Faker(locale)
    if seed is not None:
        Faker.seed(seed)
        fake.seed_instance(seed)
        random.seed(seed)
    return fake
