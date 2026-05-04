from __future__ import annotations

from lambdas.shared.faker_setup import make_faker


def test_seeded_faker_is_deterministic():
    a = make_faker(seed=42)
    b = make_faker(seed=42)
    assert a.email() == b.email()
    assert a.first_name() == b.first_name()


def test_unseeded_faker_works():
    fake = make_faker()
    assert isinstance(fake.email(), str)
