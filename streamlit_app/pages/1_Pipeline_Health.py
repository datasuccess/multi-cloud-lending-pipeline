"""Pipeline Health — invariants you'd actually want at 3am.

Drill-down companion to Home: per-day stats, success vs failure mix, anomaly
breakdown (when test mode is on), and duration trend.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from streamlit_app.lib.config import load_config
from streamlit_app.lib.data import list_runs

st.set_page_config(page_title="Pipeline health", layout="wide")
st.title("🩺 Pipeline health")

cfg = load_config()

days = st.slider("Lookback (days)", min_value=1, max_value=30, value=14)
runs = list_runs(cfg.raw_bucket, cfg.region, "loan_applications", days=days)

if runs.empty:
    st.info("No runs yet. Trigger one with the smoke-test script.")
    st.stop()

runs = runs.copy()
runs["day"] = runs["started_at"].dt.floor("D")
runs["hour"] = runs["started_at"].dt.floor("h")

# ---- Headline -------------------------------------------------------------
total = len(runs)
ok = int((runs["status"] == "success").sum())
fail = int((runs["status"] == "failure").sum())
ok_rate = ok / total if total else 0.0

c1, c2, c3, c4 = st.columns(4)
c1.metric("Runs", f"{total}")
c2.metric("Success", f"{ok}")
c3.metric("Failure", f"{fail}")
c4.metric("Success rate", f"{ok_rate:.1%}")

# ---- Hourly run timeline --------------------------------------------------
st.subheader("Runs over time")
timeline = (
    runs.groupby(["hour", "status"], observed=True)
    .size()
    .reset_index(name="count")
)
fig = px.bar(timeline, x="hour", y="count", color="status", barmode="stack")
fig.update_layout(height=320, margin={"l": 0, "r": 0, "t": 10, "b": 0})
st.plotly_chart(fig, use_container_width=True)

# ---- Volume + duration ----------------------------------------------------
st.subheader("Rows written per run")
fig = px.scatter(
    runs,
    x="started_at",
    y="rows",
    color="status",
    hover_data=["run_id", "trigger", "duration_ms"],
)
fig.update_layout(height=320, margin={"l": 0, "r": 0, "t": 10, "b": 0})
st.plotly_chart(fig, use_container_width=True)

st.subheader("Duration (ms) per run")
fig = px.scatter(
    runs,
    x="started_at",
    y="duration_ms",
    color="status",
    hover_data=["run_id", "trigger", "rows"],
)
fig.update_layout(height=320, margin={"l": 0, "r": 0, "t": 10, "b": 0})
st.plotly_chart(fig, use_container_width=True)

# ---- Failure detail -------------------------------------------------------
st.subheader("Failure detail")
fails = runs[runs["status"] == "failure"]
if fails.empty:
    st.success("No failures in this window.")
else:
    show = fails[["started_at", "run_id", "trigger", "rows", "error"]]
    st.dataframe(show, hide_index=True, use_container_width=True)

# ---- Trigger mix (handy in test mode where chaos triggers run alongside) --
st.subheader("Trigger mix")
trigger = (
    runs.groupby("trigger", observed=True)
    .size()
    .rename("count")
    .reset_index()
    .sort_values("count", ascending=False)
)
st.dataframe(trigger, hide_index=True, use_container_width=True)
