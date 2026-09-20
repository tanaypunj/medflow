"""Arrival-pattern controls for MedFlow.

The Streamlit app can use ``arrival_pattern_config`` and ``next_arrival_minutes``
to expose a menu without coupling pattern behavior to patient generation.
"""

import random

ARRIVAL_PATTERNS = {
    "Steady": {
        "label": "Steady — regular arrivals",
        "description": "Arrivals occur at a consistent interval.",
    },
    "Random": {
        "label": "Random — variable arrivals",
        "description": "Intervals vary around the configured average.",
    },
    "Peak hours": {
        "label": "Peak hours — busier during simulated daytime",
        "description": "Arrival rates increase during the simulated daytime peak.",
    },
    "Emergency waves": {
        "label": "Emergency waves — clustered arrivals",
        "description": "Patients arrive in short bursts followed by quieter periods.",
    },
}


def arrival_pattern_config(pattern):
    """Return display metadata for a supported arrival pattern."""
    return ARRIVAL_PATTERNS.get(pattern, ARRIVAL_PATTERNS["Steady"])


def next_arrival_minutes(pattern, base_interval, simulated_hour=12, rng=None):
    """Return the next arrival interval in simulated minutes.

    ``base_interval`` remains the average interval. The returned value is always
    positive, so it is safe to use as the simulation accumulator threshold.
    """
    interval = max(0.1, float(base_interval))
    rng = random if rng is None else rng

    if pattern == "Random":
        # Exponential intervals preserve the configured average while creating
        # realistic variation in arrival timing.
        return max(0.1, rng.expovariate(1.0 / interval))
    if pattern == "Peak hours":
        peak_factor = 0.55 if 8 <= int(simulated_hour) < 18 else 1.8
        return max(0.1, interval * peak_factor)
    if pattern == "Emergency waves":
        # The caller can repeatedly request intervals; short intervals model a
        # wave and longer intervals model the quiet period between waves.
        return max(0.1, interval * (0.35 if rng.random() < 0.65 else 2.5))
    return interval
