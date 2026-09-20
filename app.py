import random
import time
from copy import deepcopy
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from scenario_features import (
    medflow_priority,
    priority_breakdown,
    scenario_config,
    scenario_totals,
    urgency_only_priority,
    utilization,
)

st.set_page_config(page_title="MedFlow", page_icon="🏥", layout="wide")

RESOURCE_DEFAULTS = {
    "Beds": 30,
    "ICU beds": 6,
    "Doctors": 8,
    "Nurses": 16,
    "Operating rooms": 3,
    "Ventilators": 5,
}
RESOURCE_NAMES = list(RESOURCE_DEFAULTS)
MODE_LABELS = {
    "Normal": "Routine operations",
    "Emergency surge": "Higher emergency arrivals and acuity",
    "Staff shortage": "Reduced clinical staffing",
    "Resource shortage": "Reduced beds and equipment capacity",
    "Disaster": "Mass-casualty event",
    "Custom": "User-controlled scenario",
}
CONDITIONS = [
    ("Cardiac event", "Critical", 4, {"Beds": 1, "ICU beds": 1, "Doctors": 1, "Nurses": 2}),
    ("Respiratory distress", "Critical", 4, {"Beds": 1, "ICU beds": 1, "Doctors": 1, "Nurses": 2, "Ventilators": 1}),
    ("Major trauma", "Urgent", 3, {"Beds": 1, "Doctors": 1, "Nurses": 2, "Operating rooms": 1}),
    ("Appendicitis", "Urgent", 3, {"Beds": 1, "Doctors": 1, "Nurses": 1, "Operating rooms": 1}),
    ("Fracture", "Routine", 2, {"Beds": 1, "Doctors": 1, "Nurses": 1}),
    ("Infection", "Routine", 2, {"Beds": 1, "Doctors": 1, "Nurses": 1}),
]
DISCHARGE_MINUTES = {"Routine": 90, "Urgent": 150, "Critical": 240}
CRITICAL_TRANSFER_MINUTES = 120


def patient_record(pid, name, condition, acuity, urgency, requirements, arrival):
    return {
        "id": pid, "name": name, "condition": condition, "acuity": acuity,
        "urgency": urgency, "arrival": arrival, "requirements": requirements.copy(),
        "base_requirements": requirements.copy(), "status": "Waiting", "location": "Waiting list",
        "admitted_at": None, "discharged_at": None, "queue_wait_seconds": 0.0,
        "reason": "Not yet assessed", "priority_breakdown": {},
    }


def seed_patients():
    now = datetime.now().replace(microsecond=0)
    names = ["A. Patel", "B. Williams", "C. Garcia", "D. Chen", "E. Smith", "F. Okafor", "G. Jones", "H. Khan"]
    return [patient_record(f"P-{i + 1:03d}", name, *CONDITIONS[i % len(CONDITIONS)], now) for i, name in enumerate(names)]


def active_config():
    return scenario_config(
        st.session_state.mode,
        st.session_state.get("custom_arrival_interval", 20),
        st.session_state.get("custom_urgency_weights"),
        st.session_state.get("custom_reductions"),
    )


def apply_scenario_capacity():
    capacities = scenario_totals(st.session_state.base_resource_totals, active_config())
    for name, total in capacities.items():
        resource = st.session_state.resources[name]
        resource["total"] = max(0, total)
        resource["failed"] = min(max(0, resource["failed"]), resource["total"])


def initialize():
    st.session_state.clock = datetime.now().replace(microsecond=0)
    st.session_state.patients = seed_patients()
    st.session_state.base_resource_totals = RESOURCE_DEFAULTS.copy()
    st.session_state.resources = {name: {"total": total, "failed": 0, "repair_at": None} for name, total in RESOURCE_DEFAULTS.items()}
    st.session_state.mode = "Normal"
    st.session_state.speed = 1.0
    st.session_state.event_log = ["Simulation initialized"]
    st.session_state.ticks = 0
    st.session_state.arrival_accumulator = 0.0
    st.session_state.last_wall_time = time.monotonic()
    st.session_state.metrics_history = []
    st.session_state.custom_arrival_interval = 20
    st.session_state.custom_urgency_weights = {"Critical": 1, "Urgent": 2, "Routine": 3}
    st.session_state.custom_reductions = {name: 0.2 for name in RESOURCE_NAMES}


def ensure_state():
    if "patients" not in st.session_state:
        initialize()
    st.session_state.setdefault("base_resource_totals", RESOURCE_DEFAULTS.copy())
    st.session_state.setdefault("resources", {name: {"total": total, "failed": 0, "repair_at": None} for name, total in RESOURCE_DEFAULTS.items()})
    st.session_state.setdefault("mode", "Normal")
    st.session_state.setdefault("speed", 1.0)
    st.session_state.setdefault("arrival_accumulator", 0.0)
    st.session_state.setdefault("last_wall_time", time.monotonic())
    st.session_state.setdefault("metrics_history", [])
    st.session_state.setdefault("custom_arrival_interval", 20)
    st.session_state.setdefault("custom_urgency_weights", {"Critical": 1, "Urgent": 2, "Routine": 3})
    st.session_state.setdefault("custom_reductions", {name: 0.2 for name in RESOURCE_NAMES})
    for patient in st.session_state.patients:
        patient.setdefault("queue_wait_seconds", patient.get("waiting_seconds", 0.0))
        patient.setdefault("urgency", {"Critical": 4, "Urgent": 3, "Routine": 2}.get(patient.get("acuity"), 2))
        patient.setdefault("location", "Waiting list")
        patient.setdefault("base_requirements", patient.get("requirements", {}).copy())
        patient.setdefault("discharged_at", None)
        patient.setdefault("reason", "Not yet assessed")
    for resource in st.session_state.resources.values():
        resource.setdefault("repair_at", None)
    apply_scenario_capacity()


def effective_capacity(name):
    resource = st.session_state.resources[name]
    return max(0, resource["total"] - resource["failed"])


def current_usage(name, patients=None):
    patients = st.session_state.patients if patients is None else patients
    return sum(p["requirements"].get(name, 0) for p in patients if p["status"] == "Admitted")


def available_capacity(name):
    return max(0, effective_capacity(name) - current_usage(name))


def stay_minutes(patient):
    if patient["admitted_at"] is None:
        return 0
    end = patient["discharged_at"] or st.session_state.clock
    return max(0, int((end - patient["admitted_at"]).total_seconds() // 60))


def resource_criticality(patient):
    weights = {"ICU beds": 3, "Ventilators": 3, "Doctors": 2, "Operating rooms": 2, "Nurses": 1, "Beds": 1}
    return sum(weights.get(name, 1) for name, amount in patient["requirements"].items() if amount > 0)


def patient_priority(patient):
    if patient["status"] != "Waiting":
        return -1.0
    return medflow_priority(patient, resource_criticality(patient))


def can_admit(patient, patients=None, resources=None):
    patients = st.session_state.patients if patients is None else patients
    resources = st.session_state.resources if resources is None else resources
    blocked = []
    for name, needed in patient["requirements"].items():
        resource = resources[name]
        effective = max(0, resource["total"] - resource["failed"])
        used = current_usage(name, patients)
        available = max(0, effective - used)
        if available < needed:
            blocked.append(f"{name} ({available}/{needed})")
    return blocked


def allocate_patients():
    count = 0
    waiting = sorted((p for p in st.session_state.patients if p["status"] == "Waiting"), key=patient_priority, reverse=True)
    for patient in waiting:
        blocked = can_admit(patient)
        if blocked:
            patient["reason"] = "Waiting for " + ", ".join(blocked)
            continue
        patient["status"] = "Admitted"
        patient["location"] = "ICU" if patient["acuity"] == "Critical" and "ICU beds" in patient["requirements"] else "Normal bed"
        patient["admitted_at"] = st.session_state.clock
        patient["reason"] = "Allocated successfully"
        patient["priority_breakdown"] = priority_breakdown(patient, resource_criticality(patient))
        count += 1
    return count


def discharge_patient(pid):
    for patient in st.session_state.patients:
        if patient["id"] == pid and patient["status"] == "Admitted":
            old_location = patient["location"]
            patient["status"] = "Discharged"
            patient["discharged_at"] = st.session_state.clock
            patient["location"] = "Discharged"
            st.session_state.event_log.insert(0, f"{pid} ({patient['name']}) discharged from {old_location}; resources released.")
            return True
    return False


def repair_resources():
    for name, resource in st.session_state.resources.items():
        if resource["repair_at"] and st.session_state.clock >= resource["repair_at"]:
            failed = resource["failed"]
            resource["failed"] = 0
            resource["repair_at"] = None
            st.session_state.event_log.insert(0, f"{name} repair completed; {failed} unit(s) restored.")


def transfer_critical_patients():
    for patient in st.session_state.patients:
        if patient["status"] != "Admitted" or patient["acuity"] != "Critical" or patient["location"] != "ICU":
            continue
        if stay_minutes(patient) < CRITICAL_TRANSFER_MINUTES or available_capacity("Beds") < 1:
            continue
        patient["requirements"] = patient["base_requirements"].copy()
        patient["requirements"].pop("ICU beds", None)
        patient["requirements"].pop("Ventilators", None)
        patient["requirements"]["Beds"] = 1
        patient["location"] = "Normal bed"
        st.session_state.event_log.insert(0, f"{patient['id']} transferred from ICU to a normal bed.")


def auto_discharge():
    count = 0
    for patient in sorted((p for p in st.session_state.patients if p["status"] == "Admitted"), key=stay_minutes, reverse=True):
        if patient["acuity"] == "Critical" and patient["location"] != "Normal bed":
            continue
        if stay_minutes(patient) >= DISCHARGE_MINUTES.get(patient["acuity"], 9999) and discharge_patient(patient["id"]):
            count += 1
    return count


def add_patient():
    number = len(st.session_state.patients) + 1
    config = active_config()
    condition, acuity, urgency, requirements = random.choices(
        CONDITIONS,
        weights=[config["urgency_weights"].get(condition[1], 1) for condition in CONDITIONS],
        k=1,
    )[0]
    patient = patient_record(f"P-{number:03d}", f"Simulated patient {number}", condition, acuity, urgency, requirements, st.session_state.clock)
    st.session_state.patients.append(patient)
    st.session_state.event_log.insert(0, f"New {acuity.lower()} patient arrived: {patient['id']} ({condition}).")


def simulate_failure():
    candidates = [name for name, resource in st.session_state.resources.items() if name != "Beds" and resource["total"] - resource["failed"] > 0]
    if not candidates:
        return
    name = random.choice(candidates)
    resource = st.session_state.resources[name]
    resource["failed"] = min(resource["total"], resource["failed"] + 1)
    if name == "ICU beds":
        hours = random.randint(1, 3)
        resource["repair_at"] = st.session_state.clock + timedelta(hours=hours)
        message = f"Unexpected ICU failure; repair due in {hours} simulated hour(s)."
    else:
        message = f"Unexpected failure: one {name} became unavailable."
    st.session_state.event_log.insert(0, message)


def record_metrics():
    waiting = [p for p in st.session_state.patients if p["status"] == "Waiting"]
    treated = [p for p in st.session_state.patients if p["status"] == "Discharged"]
    entry = {
        "time": st.session_state.clock,
        "Average waiting time": sum(p["queue_wait_seconds"] for p in waiting) / len(waiting) / 60 if waiting else 0,
        "Maximum waiting time": max((p["queue_wait_seconds"] for p in waiting), default=0) / 60,
        "Critical patients waiting": sum(p["acuity"] == "Critical" for p in waiting),
        "Patients treated": len(treated),
        "Throughput": len(treated),
    }
    for name in RESOURCE_NAMES:
        entry[f"Utilization: {name}"] = utilization(current_usage(name), effective_capacity(name))
    st.session_state.metrics_history.append(entry)
    st.session_state.metrics_history = st.session_state.metrics_history[-300:]


def advance_simulation(seconds):
    steps = max(1, int(seconds // 30))
    for _ in range(steps):
        st.session_state.clock += timedelta(seconds=30)
        for patient in st.session_state.patients:
            if patient["status"] == "Waiting":
                patient["queue_wait_seconds"] += 30
                patient["reason"] = "Waiting in queue"
        st.session_state.arrival_accumulator += 0.5
        interval = active_config()["arrival_interval"]
        while st.session_state.arrival_accumulator >= interval:
            st.session_state.arrival_accumulator -= interval
            add_patient()
        repair_resources()
        transfer_critical_patients()
        auto_discharge()
        allocate_patients()
        record_metrics()
    st.session_state.ticks += steps


def update_from_real_clock():
    now = time.monotonic()
    elapsed = max(0.0, min(now - st.session_state.last_wall_time, 5.0))
    st.session_state.last_wall_time = now
    if elapsed > 0:
        advance_simulation(elapsed * st.session_state.speed)


def strategy_snapshot(strategy):
    patients = deepcopy(st.session_state.patients)
    resources = deepcopy(st.session_state.resources)
    waiting = [p for p in patients if p["status"] == "Waiting"]

    def priority(patient):
        if strategy == "Urgency Only":
            return urgency_only_priority(patient)
        return medflow_priority(patient, resource_criticality(patient))

    for patient in sorted(waiting, key=priority, reverse=True):
        blocked = can_admit(patient, patients, resources)
        if blocked:
            continue
        patient["status"] = "Admitted"
        patient["location"] = "ICU" if "ICU beds" in patient["requirements"] and patient["acuity"] == "Critical" else "Normal bed"

    remaining = [p for p in patients if p["status"] == "Waiting"]
    waits = [p["queue_wait_seconds"] / 60 for p in remaining]
    critical_wait = sum(p["queue_wait_seconds"] / 60 for p in remaining if p["acuity"] == "Critical")
    resource_utils = []
    for name, resource in resources.items():
        effective = max(0, resource["total"] - resource["failed"])
        resource_utils.append(utilization(current_usage(name, patients), effective))
    treated = sum(p["status"] == "Admitted" for p in patients)
    return {
        "Average Wait": sum(waits) / len(waits) if waits else 0.0,
        "Maximum Wait": max(waits, default=0.0),
        "Critical Wait": critical_wait,
        "Patients Treated": treated,
        "Throughput": treated,
        "Average Resource Utilization": sum(resource_utils) / len(resource_utils) if resource_utils else 0.0,
    }


ensure_state()

st.sidebar.title("⚙️ Simulation controls")
mode = st.sidebar.selectbox("Operating mode", list(MODE_LABELS), index=list(MODE_LABELS).index(st.session_state.mode), format_func=lambda x: f"{x} — {MODE_LABELS[x]}")
if mode != st.session_state.mode:
    st.session_state.mode = mode
    st.session_state.arrival_accumulator = 0.0
    apply_scenario_capacity()
    st.session_state.event_log.insert(0, f"Scenario changed to {mode}; temporary capacities recalculated.")

if st.session_state.mode == "Custom":
    st.session_state.custom_arrival_interval = st.sidebar.slider("Arrival interval (minutes)", 1, 120, int(st.session_state.custom_arrival_interval))
    st.session_state.custom_urgency_weights = {acuity: st.sidebar.slider(f"{acuity} arrival weight", 1, 10, int(st.session_state.custom_urgency_weights[acuity])) for acuity in ("Critical", "Urgent", "Routine")}
    st.session_state.custom_reductions = {name: st.sidebar.slider(f"{name} reduction", 0.0, 0.8, float(st.session_state.custom_reductions[name]), 0.05) for name in RESOURCE_NAMES}
    apply_scenario_capacity()

speed = st.session_state.get("speed", 1.0)
if isinstance(speed, str):
    speed = speed.strip("x×")
st.session_state.speed = st.sidebar.number_input("Simulation speed", 0.1, 500.0, float(speed), 0.1, format="%.1f")

for column, minutes in zip(st.sidebar.columns(5), (1, 5, 10, 15, 20)):
    if column.button(f"+{minutes}m", key=f"skip_{minutes}", use_container_width=True):
        advance_simulation(minutes * 60)
        st.session_state.last_wall_time = time.monotonic()
        st.rerun()
if st.sidebar.button("⚡ Simulate unexpected failure", use_container_width=True):
    simulate_failure()
    st.rerun()
if st.sidebar.button("🔄 Reset simulation", use_container_width=True):
    initialize()
    st.rerun()

st_autorefresh(interval=1000, key="medflow_clock")
update_from_real_clock()

st.title("🏥 MedFlow")
st.caption("Hospital management and resource-allocation simulation")
st.info(f"Simulation time: **{st.session_state.clock:%Y-%m-%d %H:%M:%S}** · Mode: **{st.session_state.mode}** · Speed: **{st.session_state.speed:.1f}×**")
st.caption("Scenario effects: " + " | ".join(f"{k}: {v}" for k, v in active_config()["summary"].items()))
st.caption("Priority formula: P = Urgency×10 + Waiting Time + Resource Criticality×2")

st.subheader("Hospital resources")
cols = st.columns(3)
for i, name in enumerate(RESOURCE_NAMES):
    with cols[i % 3]:
        new_base = st.number_input(f"{name} — base total", 0, 200, int(st.session_state.base_resource_totals[name]), key=f"base_{name}")
        if new_base != st.session_state.base_resource_totals[name]:
            st.session_state.base_resource_totals[name] = new_base
            apply_scenario_capacity()
        effective = effective_capacity(name)
        used = current_usage(name)
        st.metric("Available", max(0, effective - used), delta=f"{used} used")
        st.caption(f"Effective capacity: {effective} · Utilization: {utilization(used, effective):.0f}%")
        if st.session_state.resources[name]["failed"]:
            st.caption(f"⚠️ Failed units: {st.session_state.resources[name]['failed']}")

waiting = [p for p in st.session_state.patients if p["status"] == "Waiting"]
admitted = [p for p in st.session_state.patients if p["status"] == "Admitted"]
discharged = [p for p in st.session_state.patients if p["status"] == "Discharged"]
m1, m2, m3, m4 = st.columns(4)
m1.metric("Waiting", len(waiting))
m2.metric("Currently admitted", len(admitted))
m3.metric("Average waiting time", f"{sum(p['queue_wait_seconds'] for p in waiting) / len(waiting) / 60 if waiting else 0:.1f} min")
m4.metric("Patients treated", len(discharged))

tabs = st.tabs(["🕒 Waiting list", "🛏️ Admitted", "✅ Discharged", "📊 Utilization", "📈 Strategy Comparison", "📋 Event log"])
with tabs[0]:
    rows = []
    for i, patient in enumerate(sorted(waiting, key=patient_priority, reverse=True), 1):
        breakdown = priority_breakdown(patient, resource_criticality(patient))
        rows.append({"#": i, "ID": patient["id"], "Patient": patient["name"], "Condition": patient["condition"], "Acuity": patient["acuity"], "Priority P": f"{breakdown['Final Priority']:.1f}", "Urgency": f"{breakdown['Urgency contribution']:.0f}", "Wait": f"{breakdown['Waiting contribution']:.1f}", "Resources": f"{breakdown['Resource contribution']:.0f}", "Queue wait": f"{patient['queue_wait_seconds'] / 60:.1f} min", "Reason": patient["reason"]})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.success("No patients are waiting.")
with tabs[1]:
    if admitted:
        selected = st.selectbox("Why was this patient selected?", [p["id"] for p in admitted])
        patient = next(p for p in admitted if p["id"] == selected)
        breakdown = priority_breakdown(patient, resource_criticality(patient))
        st.write(f"**{patient['name']} — {patient['condition']}**")
        st.write(f"Urgency score: {patient['urgency']} · Waiting time: {patient['queue_wait_seconds'] / 60:.1f} min · Resource criticality: {resource_criticality(patient)}")
        st.write(f"{breakdown['Urgency contribution']:.1f} + {breakdown['Waiting contribution']:.1f} + {breakdown['Resource contribution']:.1f} = **{breakdown['Final Priority']:.1f}**")
        selected_ids = st.multiselect("Patients to discharge", [p["id"] for p in admitted])
        if st.button("✅ Discharge selected patient(s)", disabled=not selected_ids):
            for pid in selected_ids:
                discharge_patient(pid)
            allocate_patients()
            st.rerun()
        st.dataframe(pd.DataFrame([{**{"ID": p["id"], "Patient": p["name"], "Location": p["location"], "Stay": f"{stay_minutes(p)} min"}, "Priority P": f"{priority_breakdown(p, resource_criticality(p))['Final Priority']:.1f}"} for p in admitted]), use_container_width=True, hide_index=True)
    else:
        st.warning("No patients are currently admitted.")
with tabs[2]:
    rows = [{"ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Queue wait": f"{p['queue_wait_seconds'] / 60:.1f} min", "Discharged": p["discharged_at"].strftime("%H:%M:%S")} for p in discharged if p.get("discharged_at")]
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No patients have been discharged yet.")
with tabs[3]:
    st.dataframe(pd.DataFrame([{ "Resource": name, "Effective": effective_capacity(name), "Used": current_usage(name), "Available": available_capacity(name), "Utilization": utilization(current_usage(name), effective_capacity(name)) } for name in RESOURCE_NAMES]), use_container_width=True, hide_index=True)
with tabs[4]:
    comparison = {"Urgency Only": strategy_snapshot("Urgency Only"), "MEDFLOW": strategy_snapshot("MEDFLOW")}
    df = pd.DataFrame([{ "Metric": metric, **{strategy: values[metric] for strategy, values in comparison.items()} } for metric in comparison["MEDFLOW"]])
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.bar_chart(df.set_index("Metric"), use_container_width=True)
    st.caption("Both strategies use the current patient queue and current resource state; only their ordering rule changes.")
with tabs[5]:
    for event in st.session_state.event_log[:12]:
        st.write(f"• {event}")

if any(p["acuity"] == "Critical" for p in waiting):
    st.warning("⚠️ Critical patient waiting. This is a simulation warning, not clinical advice.")
