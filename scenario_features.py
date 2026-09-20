"""Scenario and strategy helpers for MedFlow.

These helpers keep scenario effects separate from the base resource totals and
make the priority formulas reusable by the main Streamlit app.
"""
from copy import deepcopy


SCENARIO_CONFIG = {
    "Normal": {
        "arrival_interval": 30,
        "urgency_weights": {"Critical": 1, "Urgent": 2, "Routine": 4},
        "resource_reduction": {},
        "summary": {"Arrival rate": "Normal", "Patient acuity": "Normal", "Resource pressure": "Normal"},
    },
    "Emergency surge": {
        "arrival_interval": 5,
        "urgency_weights": {"Critical": 4, "Urgent": 5, "Routine": 1},
        "resource_reduction": {},
        "summary": {"Arrival rate": "High", "Patient acuity": "High", "Resource pressure": "High"},
    },
    "Staff shortage": {
        "arrival_interval": 15,
        "urgency_weights": {"Critical": 2, "Urgent": 3, "Routine": 2},
        "resource_reduction": {"Doctors": 0.50, "Nurses": 0.50},
        "summary": {"Arrival rate": "Elevated", "Patient acuity": "Normal", "Resource pressure": "Doctors and nurses reduced"},
    },
    "Resource shortage": {
        "arrival_interval": 12,
        "urgency_weights": {"Critical": 2, "Urgent": 3, "Routine": 2},
        "resource_reduction": {"Beds": 0.30, "ICU beds": 0.50, "Operating rooms": 0.50, "Ventilators": 0.40},
        "summary": {"Arrival rate": "Elevated", "Patient acuity": "Normal", "Resource pressure": "Beds and equipment reduced"},
    },
    "Disaster": {
        "arrival_interval": 2,
        "urgency_weights": {"Critical": 7, "Urgent": 7, "Routine": 1},
        "resource_reduction": {},
        "summary": {"Arrival rate": "Very high", "Patient acuity": "Mass casualty", "Resource pressure": "Very high"},
    },
    "Custom": {
        "arrival_interval": 20,
        "urgency_weights": {"Critical": 1, "Urgent": 2, "Routine": 3},
        "resource_reduction": {},
        "summary": {"Arrival rate": "User controlled", "Patient acuity": "User controlled", "Resource pressure": "User controlled"},
    },
}


def scenario_config(name, custom_interval=20, custom_weights=None, custom_reductions=None):
    """Return a fresh scenario configuration without mutating global defaults."""
    config = deepcopy(SCENARIO_CONFIG.get(name, SCENARIO_CONFIG["Normal"]))
    if name == "Custom":
        config["arrival_interval"] = max(1, int(custom_interval))
        if custom_weights:
            config["urgency_weights"] = dict(custom_weights)
        if custom_reductions:
            config["resource_reduction"] = {
                resource: min(0.95, max(0.0, float(reduction)))
                for resource, reduction in custom_reductions.items()
            }
    return config


def scenario_totals(base_totals, config):
    """Calculate temporary scenario capacity from immutable base totals."""
    return {
        name: max(0, int(round(total * (1 - config["resource_reduction"].get(name, 0.0)))))
        for name, total in base_totals.items()
    }


def urgency_only_priority(patient):
    return patient.get("urgency", 0) * 10


def medflow_priority(patient, resource_criticality):
    """MEDFLOW: U×10 + W + R×2, with W expressed in minutes."""
    wait_minutes = patient.get("queue_wait_seconds", 0.0) / 60
    urgency = patient.get("urgency", 0)
    return urgency * 10 + wait_minutes + resource_criticality * 2


def priority_breakdown(patient, resource_criticality):
    urgency = patient.get("urgency", 0) * 10
    waiting = patient.get("queue_wait_seconds", 0.0) / 60
    resources = resource_criticality * 2
    return {
        "Urgency contribution": urgency,
        "Waiting contribution": waiting,
        "Resource contribution": resources,
        "Final Priority": urgency + waiting + resources,
    }


def utilization(used, effective):
    return used / effective * 100 if effective > 0 else 0.0
