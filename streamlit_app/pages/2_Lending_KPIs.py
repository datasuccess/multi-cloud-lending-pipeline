"""Lending KPIs — read latest successful partition + slice by the dimensions
a credit / capital team actually cares about.
"""

from __future__ import annotations

import plotly.express as px
import streamlit as st

from lib.config import load_config
from lib.data import latest_parquet
from lib.kpis import (
    amount_distribution,
    channel_mix,
    dti_distribution,
    employment_mix,
    headline_kpis,
    purpose_mix,
    state_concentration,
    term_mix,
)

st.set_page_config(page_title="Lending KPIs", layout="wide")
st.title("📊 Lending KPIs")

cfg = load_config()

df = latest_parquet(cfg.raw_bucket, cfg.region, "loan_applications")
if df is None or df.empty:
    st.warning("No successful partitions yet.")
    st.stop()

ingest_date = df.attrs.get("ingest_date", "?")
st.caption(f"Latest partition: **ingest_date={ingest_date}** · {len(df):,} applications")

kpi = headline_kpis(df)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Applications", f"{kpi.applications:,}")
c2.metric("Total requested", f"${kpi.total_requested_usd:,.0f}")
c3.metric("Median amount", f"${kpi.median_amount_usd:,.0f}")
c4.metric("P95 amount", f"${kpi.p95_amount_usd:,.0f}")

c5, c6, c7, c8 = st.columns(4)
c5.metric("Median income", f"${kpi.median_income_usd:,.0f}")
c6.metric("Median DTI", f"{kpi.median_dti:.0%}")
c7.metric("DTI > 43%", f"{kpi.high_dti_share:.1%}", help="CFPB QM cutoff")
c8.metric("Unemployed share", f"{kpi.unemployed_share:.1%}")

# ---- Amount distribution --------------------------------------------------
st.subheader("Loan amount distribution")
amt = amount_distribution(df, bins=40)
fig = px.bar(amt, x="loan_amount_usd", y="applications")
fig.update_layout(height=300, margin={"l": 0, "r": 0, "t": 10, "b": 0})
st.plotly_chart(fig, use_container_width=True)

# ---- DTI bands ------------------------------------------------------------
st.subheader("DTI bands (existing_debt / annual_income)")
dti = dti_distribution(df)
fig = px.bar(dti, x="dti_band", y="applications", text="share")
fig.update_traces(texttemplate="%{text:.1%}", textposition="outside")
fig.update_layout(height=300, margin={"l": 0, "r": 0, "t": 10, "b": 30})
st.plotly_chart(fig, use_container_width=True)

# ---- Mix sections ---------------------------------------------------------
left, right = st.columns(2)
with left:
    st.subheader("Channel mix")
    st.dataframe(channel_mix(df), hide_index=True, use_container_width=True)
    st.subheader("Purpose mix")
    st.dataframe(purpose_mix(df), hide_index=True, use_container_width=True)

with right:
    st.subheader("Employment mix")
    st.dataframe(employment_mix(df), hide_index=True, use_container_width=True)
    st.subheader("Term mix (months)")
    st.dataframe(term_mix(df), hide_index=True, use_container_width=True)

# ---- Geo concentration ----------------------------------------------------
st.subheader("Top 10 US states")
states = state_concentration(df, top_n=10)
fig = px.bar(states, x="state", y="applications", text="share")
fig.update_traces(texttemplate="%{text:.1%}", textposition="outside")
fig.update_layout(height=320, margin={"l": 0, "r": 0, "t": 10, "b": 30})
st.plotly_chart(fig, use_container_width=True)
