"""Unit tests for the anomaly injection engine.

Pure logic — no AWS, no filesystem. Uses seeded `random.Random` so dice
rolls are deterministic.
"""

from __future__ import annotations

import random

import pytest

from lambdas.shared.anomaly import (
    DEFAULT_PROBS,
    Anomaly,
    AnomalyConfig,
    pick_anomaly,
    undershoot_rows,
)


def test_config_disabled_in_prod_mode_by_default():
    cfg = AnomalyConfig.from_env(env={})
    assert cfg.enabled is False


def test_config_enabled_when_mode_test():
    cfg = AnomalyConfig.from_env(env={"MODE": "test"})
    assert cfg.enabled is True
    assert cfg.probs == DEFAULT_PROBS


def test_config_overrides_per_anomaly_probability():
    cfg = AnomalyConfig.from_env(
        env={"MODE": "test", "ANOMALY_SKIP_PROB": "0.5", "ANOMALY_SLOW_PROB": "0"}
    )
    assert cfg.probs[Anomaly.SKIP] == 0.5
    assert cfg.probs[Anomaly.SLOW] == 0.0
    # Unspecified ones keep their defaults.
    assert cfg.probs[Anomaly.UNDERSHOOT] == DEFAULT_PROBS[Anomaly.UNDERSHOOT]


def test_pick_anomaly_returns_none_when_disabled():
    cfg = AnomalyConfig(enabled=False, probs=DEFAULT_PROBS)
    # Even a fully-deterministic 0.0 roll must return NONE when disabled.
    assert pick_anomaly(cfg, rng=random.Random(0)) is Anomaly.NONE


@pytest.mark.parametrize(
    "anomaly",
    [Anomaly.SKIP, Anomaly.UNDERSHOOT, Anomaly.SILENT_FAIL, Anomaly.SLOW],
)
def test_pick_anomaly_can_return_each_type(anomaly):
    """Force probability=1.0 for one anomaly, 0.0 for the others."""
    probs = {a: 0.0 for a in DEFAULT_PROBS}
    probs[anomaly] = 1.0
    cfg = AnomalyConfig(enabled=True, probs=probs)
    assert pick_anomaly(cfg, rng=random.Random(0)) is anomaly


def test_pick_anomaly_returns_none_when_roll_exceeds_cumulative():
    """Default probs sum < 1.0; remainder must produce NONE."""
    cfg = AnomalyConfig(enabled=True, probs=DEFAULT_PROBS)

    class FixedRng:
        def random(self) -> float:
            return 0.99  # well above sum of default probs (~0.23)

    assert pick_anomaly(cfg, rng=FixedRng()) is Anomaly.NONE


def test_undershoot_rows_in_documented_range():
    rng = random.Random(123)
    for _ in range(50):
        n = undershoot_rows(rng)
        assert 100 <= n <= 450
