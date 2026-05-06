"""Streamlit-side config loader.

Pulls bucket / region / alarm names from `lending/<env>/streamlit-config` in
Secrets Manager via the shared helper. Cached in `st.session_state` so we
hit Secrets Manager once per browser session, not once per page render.
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from lambdas.shared.secrets import get_secret


@dataclass(frozen=True)
class AppConfig:
    raw_bucket: str
    region: str
    lambda_name: str
    alarm_errors: str
    alarm_freshness: str
    alarm_low_volume: str
    powertools_namespace: str
    powertools_service: str

    @classmethod
    def from_secret(cls, env: str = "dev") -> "AppConfig":
        payload = get_secret(f"lending/{env}/streamlit-config")
        return cls(
            raw_bucket=payload["raw_bucket"],
            region=payload["region"],
            lambda_name=payload["lambda_name"],
            alarm_errors=payload["alarm_errors"],
            alarm_freshness=payload["alarm_freshness"],
            alarm_low_volume=payload["alarm_low_volume"],
            powertools_namespace=payload["powertools_namespace"],
            powertools_service=payload["powertools_service"],
        )


def load_config(env: str = "dev") -> AppConfig:
    key = f"_app_config_{env}"
    if key not in st.session_state:
        st.session_state[key] = AppConfig.from_secret(env)
    return st.session_state[key]
