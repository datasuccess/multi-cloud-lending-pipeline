"""Synthetic `delinquencies` snapshot — derived, not invented.

Inputs:
- drawdowns: drawdown_id, customer_id, drawn_amount (FK + outstanding-principal base).
- payments history (across all partitions ≤ as_of_date): per-drawdown
  scheduled_amount, actual_amount, principal_amount, scheduled_at,
  payment_status.

Output: one row per drawdown whose cumulative scheduled minus cumulative
actual is positive at `as_of_date`. Per docs/02-fan-out.md §13.6:

    1–30  → "1-30"
    31–60 → "31-60"
    61–90 → "61-90"
    >90   → "90+"

DPD anchor: the earliest scheduled_at where the running gap turned
positive and stayed positive through the snapshot. `dpd_days` is
`(as_of_date - that_date).days`. Drawdowns whose cumulative gap is zero
(every period paid in full or made up by overpayment) emit nothing.

Outstanding principal: `drawn_amount - sum(principal_amount across history)`,
floored at 0. This is approximate — it ignores compounding — but Phase 2
isn't asked to validate financial mathematics; Phase 5 dbt is the
production-style enforcement layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

GENERATOR_VERSION = "delinquencies/0.1.0"
SOURCE = "delinquencies"

DPD_BUCKETS = ("1-30", "31-60", "61-90", "90+")


def _bucket_for(dpd_days: int) -> str:
    if dpd_days <= 30:
        return "1-30"
    if dpd_days <= 60:
        return "31-60"
    if dpd_days <= 90:
        return "61-90"
    return "90+"


def _coerce_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _earliest_unpaid_anchor(payments: list[dict]) -> date | None:
    """Earliest scheduled_at where running (scheduled - actual) turned positive
    and stayed positive through the latest payment.

    `payments` is one drawdown's history, sorted by scheduled_at ascending.
    Returns None if the running gap is zero at the end (not delinquent).
    """
    running = Decimal("0.00")
    anchor: date | None = None
    for p in payments:
        scheduled = _coerce_decimal(p["scheduled_amount"])
        actual = _coerce_decimal(p["actual_amount"])
        running += scheduled - actual
        if running > 0 and anchor is None:
            anchor = p["scheduled_at"]
        elif running <= 0:
            anchor = None
    return anchor


def make_rows(
    drawdowns: Iterable[dict],
    payments_by_drawdown: dict[str, list[dict]],
    *,
    as_of_date: date,
    ingest_at: datetime | None = None,
) -> list[dict]:
    """Build snapshot rows. Deterministic — no rng required.

    `payments_by_drawdown` maps drawdown_id → list of payment dicts (any
    order; we sort here). Drawdowns with no payment history yet, or with
    a zero cumulative gap, do not produce a row.
    """
    ingest_at = ingest_at or datetime.now(timezone.utc)
    rows: list[dict] = []

    for d in drawdowns:
        drawdown_id = d["drawdown_id"]
        history = payments_by_drawdown.get(drawdown_id) or []
        if not history:
            continue

        history_sorted = sorted(history, key=lambda p: p["scheduled_at"])
        anchor = _earliest_unpaid_anchor(history_sorted)
        if anchor is None:
            continue

        dpd_days = max((as_of_date - anchor).days, 1)
        principal_paid = sum(
            (_coerce_decimal(p["principal_amount"]) for p in history_sorted),
            Decimal("0.00"),
        )
        outstanding = _coerce_decimal(d["drawn_amount"]) - principal_paid
        if outstanding < 0:
            outstanding = Decimal("0.00")

        rows.append(
            {
                "snapshot_id": str(uuid4()),
                "drawdown_id": drawdown_id,
                "customer_id": d["customer_id"],
                "dpd_days": int(dpd_days),
                "dpd_bucket": _bucket_for(dpd_days),
                "outstanding_principal": Decimal(f"{float(outstanding):.2f}"),
                "as_of_date": as_of_date,
                "_generator_version": GENERATOR_VERSION,
                "_ingest_at": ingest_at,
            }
        )

    return rows
