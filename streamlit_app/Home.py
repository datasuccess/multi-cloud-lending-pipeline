"""Lending pipeline monitoring — landing page.

Snapshot of "is the pipeline healthy right now": alarm states, the latest
successful partition, and a 14-day run history. Deeper drill-downs live on
the page-named sub-pages (auto-discovered from `pages/`).
"""

from __future__ import annotations

import streamlit as st

from streamlit_app.lib.config import load_config
from streamlit_app.lib.data import alarm_states, list_partitions, list_runs

st.set_page_config(page_title="Lending pipeline", layout="wide")

st.title("Lending pipeline · monitoring")
st.caption("Phase 1 — loan_applications generator. New pages will land per phase.")

cfg = load_config()
st.sidebar.markdown(f"**Bucket:** `{cfg.raw_bucket}`")
st.sidebar.markdown(f"**Region:** `{cfg.region}`")
st.sidebar.markdown(f"**Lambda:** `{cfg.lambda_name}`")
st.sidebar.markdown("---")
st.sidebar.markdown(
    "Pages:\n"
    "- 🩺 Pipeline Health\n"
    "- 📊 Lending KPIs\n"
)

# ---- Alarms ---------------------------------------------------------------
st.subheader("Alarm states")
alarms = alarm_states(
    cfg.region,
    [cfg.alarm_errors, cfg.alarm_freshness, cfg.alarm_low_volume],
)
if alarms.empty:
    st.info("No alarms found yet. Run `infra/02-setup-monitoring.sh`.")
else:
    cols = st.columns(len(alarms))
    for col, (_, row) in zip(cols, alarms.iterrows(), strict=True):
        emoji = {"OK": "🟢", "ALARM": "🔴", "INSUFFICIENT_DATA": "⚪"}.get(
            row["state"], "⚪"
        )
        col.metric(row["alarm"], f"{emoji} {row['state']}")
        col.caption(row["reason"][:120])

# ---- Latest partition -----------------------------------------------------
st.subheader("Latest partitions")
parts = list_partitions(cfg.raw_bucket, cfg.region, "loan_applications")
if parts.empty:
    st.warning("No partitions yet. Run a smoke test.")
else:
    parts_display = parts.head(7).copy()
    parts_display["parquet_mb"] = (parts_display["parquet_bytes"] / 1024 / 1024).round(2)
    parts_display = parts_display[
        ["ingest_date", "has_success", "parquet_files", "parquet_mb"]
    ]
    st.dataframe(parts_display, hide_index=True, use_container_width=True)

# ---- Recent runs ----------------------------------------------------------
st.subheader("Last 14 days · runs ledger")
runs = list_runs(cfg.raw_bucket, cfg.region, "loan_applications", days=14)
if runs.empty:
    st.info("No runs in the ledger.")
else:
    show = runs[
        ["started_at", "status", "rows", "bytes", "duration_ms", "trigger", "run_id"]
    ].copy()
    show["mb"] = (show["bytes"] / 1024 / 1024).round(2)
    st.dataframe(
        show.drop(columns=["bytes"]),
        hide_index=True,
        use_container_width=True,
    )
