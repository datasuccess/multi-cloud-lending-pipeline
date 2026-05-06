"""Anomaly injection engine for chaos / monitoring tests.

`MODE=test` enables four mutually-exclusive anomalies, each gated by an env
var probability. `MODE=prod` (default) returns `Anomaly.NONE` — production
runs are deterministic.

Designed so the handler imports one helper (`pick_anomaly`) and the rest of
the file is pure logic that's testable without touching AWS.

See `docs/anomaly-injection.md` for the operating philosophy.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from enum import Enum


class Anomaly(str, Enum):
    NONE = "none"
    SKIP = "skip"               # return early, no artefacts, no metrics → freshness alarm
    UNDERSHOOT = "undershoot"   # write 100-300 rows instead of N → low-volume alarm
    SILENT_FAIL = "silent_fail" # raise after writing parquet/manifest → errors alarm
    SLOW = "slow"               # sleep mid-run, then succeed → duration widget


# Defaults tuned for ~3-5 events/day under hourly invocation.
DEFAULT_PROBS: dict[Anomaly, float] = {
    Anomaly.SKIP: 0.03,
    Anomaly.UNDERSHOOT: 0.10,
    Anomaly.SILENT_FAIL: 0.05,
    Anomaly.SLOW: 0.05,
}


@dataclass(frozen=True)
class AnomalyConfig:
    enabled: bool
    probs: dict[Anomaly, float]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AnomalyConfig":
        env = env if env is not None else os.environ
        enabled = env.get("MODE", "prod").lower() == "test"
        probs = {
            a: float(env.get(f"ANOMALY_{a.value.upper()}_PROB", DEFAULT_PROBS[a]))
            for a in DEFAULT_PROBS
        }
        return cls(enabled=enabled, probs=probs)


def pick_anomaly(
    cfg: AnomalyConfig | None = None,
    rng: random.Random | None = None,
) -> Anomaly:
    """Roll once, pick at most one anomaly. Returns Anomaly.NONE when disabled."""
    cfg = cfg or AnomalyConfig.from_env()
    if not cfg.enabled:
        return Anomaly.NONE
    rng = rng or random.Random()

    roll = rng.random()
    cumulative = 0.0
    for a in (Anomaly.SKIP, Anomaly.UNDERSHOOT, Anomaly.SILENT_FAIL, Anomaly.SLOW):
        cumulative += cfg.probs[a]
        if roll < cumulative:
            return a
    return Anomaly.NONE


def undershoot_rows(rng: random.Random | None = None) -> int:
    """How many rows to write under an UNDERSHOOT anomaly.

    Range deliberately straddles the test-mode low-volume alarm threshold
    (400) so most undershoots breach but a few squeak through — that's
    realistic for "partial-batch" failures we'd see in real life.
    """
    rng = rng or random.Random()
    return rng.randint(100, 450)


SLOW_SLEEP_SECONDS = 25
