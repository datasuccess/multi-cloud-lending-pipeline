"""Production-style lending KPIs computed from a partition's parquet.

What a fraud / credit / capital team would actually watch in raw applications
(before scoring): volume, amount distribution, DTI, channel mix, employment
mix, purpose mix, geographic concentration. The schema declares
`status="submitted"` for every row at this stage, so decision/approval
metrics will land in a Phase-3 dbt mart, not here.

All metrics are deterministic functions of the dataframe — pandas only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class HeadlineKPIs:
    applications: int
    total_requested_usd: float
    median_amount_usd: float
    p95_amount_usd: float
    median_income_usd: float
    median_dti: float
    high_dti_share: float          # share of apps with DTI > 0.43 (CFPB QM cutoff)
    nonus_share: float
    unemployed_share: float


def _to_float(s: pd.Series) -> pd.Series:
    """pyarrow decimals come through as `Decimal`; cast for math."""
    if len(s) and isinstance(s.iloc[0], Decimal):
        return s.astype(float)
    return pd.to_numeric(s, errors="coerce")


def _dti_series(df: pd.DataFrame) -> pd.Series:
    income = _to_float(df["annual_income"]).replace(0, np.nan)
    debt = _to_float(df["existing_debt"])
    return (debt / income).fillna(0.0)


def headline_kpis(df: pd.DataFrame) -> HeadlineKPIs:
    if df.empty:
        return HeadlineKPIs(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    amounts = _to_float(df["amount_requested"])
    income = _to_float(df["annual_income"])
    dti = _dti_series(df)
    return HeadlineKPIs(
        applications=len(df),
        total_requested_usd=float(amounts.sum()),
        median_amount_usd=float(amounts.median()),
        p95_amount_usd=float(np.percentile(amounts, 95)),
        median_income_usd=float(income.median()),
        median_dti=float(dti.median()),
        high_dti_share=float((dti > 0.43).mean()),
        nonus_share=float((df["country"] != "US").mean()),
        unemployed_share=float((df["employment_status"] == "unemployed").mean()),
    )


def _share(df: pd.DataFrame, col: str) -> pd.DataFrame:
    out = df.groupby(col, observed=True).size().rename("applications").reset_index()
    out["share"] = out["applications"] / out["applications"].sum()
    return out.sort_values("applications", ascending=False)


def channel_mix(df: pd.DataFrame) -> pd.DataFrame:
    return _share(df, "channel")


def purpose_mix(df: pd.DataFrame) -> pd.DataFrame:
    return _share(df, "purpose")


def employment_mix(df: pd.DataFrame) -> pd.DataFrame:
    return _share(df, "employment_status")


def term_mix(df: pd.DataFrame) -> pd.DataFrame:
    return _share(df, "term_months")


def state_concentration(df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    out = (
        df[df["country"] == "US"]
        .groupby("state", observed=True)
        .size()
        .rename("applications")
        .reset_index()
        .sort_values("applications", ascending=False)
        .head(top_n)
    )
    out["share"] = out["applications"] / len(df)
    return out


def amount_distribution(df: pd.DataFrame, bins: int = 30) -> pd.DataFrame:
    amounts = _to_float(df["amount_requested"])
    counts, edges = np.histogram(amounts, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2
    return pd.DataFrame({"loan_amount_usd": centers, "applications": counts})


def dti_distribution(df: pd.DataFrame) -> pd.DataFrame:
    dti = _dti_series(df)
    bins = [0, 0.1, 0.2, 0.3, 0.43, 0.6, 1.0, np.inf]
    labels = ["0-10%", "10-20%", "20-30%", "30-43%", "43-60%", "60-100%", ">100%"]
    bucketed = pd.cut(dti, bins=bins, labels=labels, include_lowest=True)
    out = (
        bucketed.value_counts()
        .rename_axis("dti_band")
        .reset_index(name="applications")
    )
    out["share"] = out["applications"] / out["applications"].sum()
    return out
