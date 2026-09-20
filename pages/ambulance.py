import streamlit as st
import pandas as pd

from arrival_patterns import ARRIVAL_PATTERNS

st.set_page_config(page_title="Ambulance | MedFlow", page_icon="🚑", layout="wide")

AMBULANCE_SHARE_BY_MODE = {
    "Normal": 0.15,
    "Emergency surge": 0.75,
    "Staff shortage": 0.25,
    "Resource shortage": 0.35,
    "Disaster": 0.90,
    "Custom": 0.45,
}

PATTERN_DETAILS = {
    "Steady": {
        "Typical behavior": "Regular arrivals at the configured interval",
        "Best used for": "Routine ambulance operations",
    },
    "Random": {
        "Typical behavior": "Variable intervals around the configured average",
        "Best used for": "Unpredictable day-to-day demand",
    },
    "Peak hours": {
        "Typical behavior": "More arrivals during simulated daytime hours",
        "Best used for": "Rush-hour or daytime demand analysis",
    },
    "Emergency waves": {
        "Typical behavior": "Short bursts followed by quieter periods",
        "Best used for": "Mass-casualty and emergency surge scenarios",
    },
}

st.title("🚑 Ambulance")
st.caption("Ambulance arrival patterns and scenario configuration")

st.subheader("Arrival pattern table")
pattern_rows = []
for pattern, metadata in ARRIVAL_PATTERNS.items():
    details = PATTERN_DETAILS.get(pattern, {})
    pattern_rows.append({
        "Pattern": pattern,
        "Description": metadata["description"],
        "Typical behavior": details.get("Typical behavior", "—"),
        "Best used for": details.get("Best used for", "—"),
    })

st.dataframe(
    pd.DataFrame(pattern_rows),
    use_container_width=True,
    hide_index=True,
)

st.subheader("Ambulance share by operating condition")
share_rows = [
    {
        "Operating condition": mode,
        "Expected ambulance arrivals": f"{share:.0%}",
        "Expected walk-in arrivals": f"{1 - share:.0%}",
    }
    for mode, share in AMBULANCE_SHARE_BY_MODE.items()
]
st.dataframe(pd.DataFrame(share_rows), use_container_width=True, hide_index=True)

st.info(
    "Normal conditions use mostly walk-in arrivals. Emergency surge and disaster "
    "conditions use mostly ambulance arrivals. Select the arrival pattern from the "
    "main simulation sidebar to change the timing behavior."
)
