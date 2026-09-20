import random
import time
from copy import deepcopy
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="MedFlow", page_icon="🏥", layout="wide")

STEP_SECONDS = 30
MAX_EVENTS = 200
MAX_HISTORY = 300
MAX_DISCHARGED_KEPT = 500
DEFAULT_SPEED = 30.0

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
    "Custom": "Custom scenario",
}
ARRIVAL_INTERVALS = {"Normal": 30, "Emergency surge": 5, "Staff shortage": 15, "Resource shortage": 12, "Disaster": 2, "Custom": 20}
MODE_CAPACITY_FACTORS = {
    "Staff shortage": {"Doctors": 0.60, "Nurses": 0.60},
    "Resource shortage": {"Beds": 0.70, "ICU beds": 0.50, "Operating rooms": 0.67, "Ventilators": 0.50},
}
CONDITION_WEIGHTS = {
    "Normal": [4, 3, 2, 2, 4, 4],
    "Emergency surge": [4, 4, 4, 3, 1, 1],
    "Staff shortage": [2, 2, 3, 3, 3, 3],
    "Resource shortage": [3, 3, 3, 3, 2, 2],
    "Disaster": [5, 4, 7, 5, 1, 1],
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
CRITICALITY_WEIGHTS = {"ICU beds": 3, "Ventilators": 3, "Doctors": 2, "Operating rooms": 2, "Nurses": 1, "Beds": 1}


def log(message):
    events = st.session_state.event_log
    events.insert(0, f"[{st.session_state.clock:%H:%M}] {message}")
    del events[MAX_EVENTS:]


def patient_record(pid, name, condition, acuity, urgency, requirements, arrival, wait_seconds=0.0):
    return {
        "id": pid, "name": name, "condition": condition, "acuity": acuity, "urgency": urgency,
        "arrival": arrival, "requirements": requirements.copy(), "base_requirements": requirements.copy(),
        "status": "Waiting", "location": "Waiting list", "admitted_at": None, "discharged_at": None,
        "queue_wait_seconds": float(wait_seconds), "reason": "Not yet assessed",
    }


def seed_patients(now):
    names = ["A. Patel", "B. Williams", "C. Garcia", "D. Chen", "E. Smith", "F. Okafor", "G. Jones", "H. Khan"]
    patients = []
    for i, name in enumerate(names):
        condition, acuity, urgency, requirements = CONDITIONS[i % len(CONDITIONS)]
        minutes_ago = (i + 1) * 12
        patients.append(patient_record(f"P-{i + 1:03d}", name, condition, acuity, urgency, requirements, now - timedelta(minutes=minutes_ago), minutes_ago * 60))
    return patients


def initialize():
    ss = st.session_state
    now = datetime.now().replace(microsecond=0)
    ss.clock = now
    ss.sim_start = now
    ss.patients = seed_patients(now)
    ss.next_patient_number = len(ss.patients) + 1
    ss.treated_total = 0
    ss.base_resource_totals = RESOURCE_DEFAULTS.copy()
    ss.resources = {name: {"total": total, "repairs": []} for name, total in RESOURCE_DEFAULTS.items()}
    ss.mode = "Normal"
    ss.last_mode = "Normal"
    ss.speed = DEFAULT_SPEED
    ss.paused = False
    ss.event_log = ["Simulation initialized"]
    ss.ticks = 0
    ss.arrival_accumulator = 0.0
    ss.sim_remainder = 0.0
    ss.last_wall_time = time.monotonic()
    ss.metrics_history = []
    ss.custom_interval = 20.0
    ss.custom_weights = {"Critical": 4, "Urgent": 4, "Routine": 2}
    ss.custom_factors = {name: 0.0 for name in RESOURCE_NAMES}
    for name in RESOURCE_DEFAULTS:
        ss.pop(f"total_{name}", None)


def ensure_state():
    if "patients" not in st.session_state:
        initialize()
    ss = st.session_state
    ss.setdefault("base_resource_totals", RESOURCE_DEFAULTS.copy())
    ss.setdefault("resources", {name: {"total": total, "repairs": []} for name, total in RESOURCE_DEFAULTS.items()})
    ss.setdefault("mode", "Normal")
    ss.setdefault("last_mode", ss.mode)
    ss.setdefault("speed", DEFAULT_SPEED)
    ss.setdefault("paused", False)
    ss.setdefault("arrival_accumulator", 0.0)
    ss.setdefault("sim_remainder", 0.0)
    ss.setdefault("last_wall_time", time.monotonic())
    ss.setdefault("metrics_history", [])
    ss.setdefault("custom_interval", 20.0)
    ss.setdefault("custom_weights", {"Critical": 4, "Urgent": 4, "Routine": 2})
    ss.setdefault("custom_factors", {name: 0.0 for name in RESOURCE_NAMES})
    for name in RESOURCE_NAMES:
        ss.resources.setdefault(name, {"total": ss.base_resource_totals.get(name, 0), "repairs": []})
    for patient in ss.patients:
        patient.setdefault("queue_wait_seconds", patient.get("waiting_seconds", 0.0))
        patient.setdefault("urgency", {"Critical": 4, "Urgent": 3, "Routine": 2}.get(patient.get("acuity"), 2))
        patient.setdefault("location", "Waiting list")
        patient.setdefault("base_requirements", patient.get("requirements", {}).copy())
        patient.setdefault("discharged_at", None)
        patient.setdefault("reason", "Not yet assessed")


def mode_config():
    ss = st.session_state
    factors = deepcopy(MODE_CAPACITY_FACTORS.get(ss.mode, {}))
    weights = CONDITION_WEIGHTS.get(ss.mode, CONDITION_WEIGHTS["Normal"])
    interval = ARRIVAL_INTERVALS.get(ss.mode, 20)
    if ss.mode == "Custom":
        interval = max(0.5, float(ss.custom_interval))
        weights = [ss.custom_weights.get(acuity, 1) for _, acuity, _, _ in CONDITIONS]
        factors = {name: min(0.95, max(0.0, value)) for name, value in ss.custom_factors.items()}
    return {"interval": interval, "weights": weights, "factors": factors}


def scenario_summary():
    config = mode_config()
    reduced = [f"{name}: {factor * 100:.0f}% reduced" for name, factor in config["factors"].items() if factor]
    acuity = "High" if st.session_state.mode in ("Emergency surge", "Disaster") else "Normal"
    arrival = "Very high" if config["interval"] <= 2 else "High" if config["interval"] <= 5 else "Normal"
    return {"Arrival rate": f"{arrival} (every {config['interval']:.1f} min)", "Patient acuity": acuity, "Resource pressure": ", ".join(reduced) or "Normal capacity"}


def failed_units(name):
    return len(st.session_state.resources[name]["repairs"])


def mode_capacity(name):
    factor = mode_config()["factors"].get(name, 0.0)
    return max(0, int(round(st.session_state.base_resource_totals[name] * (1 - factor))))


def effective_capacity(name, resources=None):
    resources = st.session_state.resources if resources is None else resources
    resource = resources[name]
    return max(0, mode_capacity(name) if resources is st.session_state.resources else int(resource["total"]) - len(resource["repairs"])) - (failed_units(name) if resources is st.session_state.resources else len(resource["repairs"])) if resources is st.session_state.resources else max(0, int(resource["total"]) - len(resource["repairs"]))


def usage_snapshot(patients=None):
    patients = st.session_state.patients if patients is None else patients
    usage = dict.fromkeys(RESOURCE_NAMES, 0)
    for patient in patients:
        if patient["status"] == "Admitted":
            for name, amount in patient["requirements"].items():
                usage[name] = usage.get(name, 0) + amount
    return usage


def available_capacity(name, usage, resources=None):
    return max(0, effective_capacity(name, resources) - usage.get(name, 0))


def utilization(name, usage, resources=None):
    capacity = effective_capacity(name, resources)
    return 100.0 if capacity == 0 and usage.get(name, 0) else (usage.get(name, 0) / capacity * 100 if capacity else 0.0)


def stay_minutes(patient):
    if patient["admitted_at"] is None:
        return 0
    end = patient["discharged_at"] or st.session_state.clock
    return max(0, int((end - patient["admitted_at"]).total_seconds() // 60))


def resource_criticality(patient):
    return sum(CRITICALITY_WEIGHTS.get(name, 1) for name, amount in patient["requirements"].items() if amount > 0)


def priority_breakdown(patient):
    urgency = patient["urgency"] * 10
    waiting = patient["queue_wait_seconds"] / 60
    resource = resource_criticality(patient) * 2
    return {"Urgency contribution": urgency, "Waiting contribution": waiting, "Resource contribution": resource, "Final Priority": urgency + waiting + resource}


def patient_priority(patient):
    if patient["status"] != "Waiting":
        return -1.0
    return priority_breakdown(patient)["Final Priority"]


def can_admit(patient, patients=None, resources=None):
    patients = st.session_state.patients if patients is None else patients
    resources = st.session_state.resources if resources is None else resources
    usage = usage_snapshot(patients)
    blocked = []
    for name, needed in patient["requirements"].items():
        free = available_capacity(name, usage, resources)
        if free < needed:
            blocked.append(f"{name} ({free}/{needed})")
    return blocked


def allocate_patients():
    ss = st.session_state
    waiting = sorted((p for p in ss.patients if p["status"] == "Waiting"), key=patient_priority, reverse=True)
    usage = usage_snapshot()
    admitted = 0
    reserved = set()
    for patient in waiting:
        short = []
        for name, needed in patient["requirements"].items():
            free = available_capacity(name, usage)
            if free < needed:
                short.append((name, needed, free))
        if short:
            patient["reason"] = "Waiting for " + ", ".join(f"{n} ({f}/{q})" for n, q, f in short)
            reserved.update(n for n, q, _ in short if q <= mode_capacity(n))
            continue
        held = [name for name in patient["requirements"] if name in reserved]
        if held:
            patient["reason"] = "Held for higher-priority patient: " + ", ".join(held)
            continue
        for name, needed in patient["requirements"].items():
            usage[name] = usage.get(name, 0) + needed
        patient["status"] = "Admitted"
        patient["location"] = "ICU" if patient["acuity"] == "Critical" and "ICU beds" in patient["requirements"] else "Normal bed"
        patient["admitted_at"] = ss.clock
        patient["reason"] = "Allocated successfully"
        admitted += 1
    return admitted


def release_patient(patient):
    old_location = patient["location"]
    patient["status"] = "Discharged"
    patient["discharged_at"] = st.session_state.clock
    patient["location"] = "Discharged"
    st.session_state.treated_total += 1
    log(f"{patient['id']} ({patient['name']}) discharged from {old_location}; resources released.")


def discharge_patient(pid):
    for patient in st.session_state.patients:
        if patient["id"] == pid and patient["status"] == "Admitted":
            release_patient(patient)
            return True
    return False


def repair_resources():
    for name, resource in st.session_state.resources.items():
        due = [t for t in resource["repairs"] if t <= st.session_state.clock]
        if due:
            resource["repairs"] = [t for t in resource["repairs"] if t > st.session_state.clock]
            log(f"{name} repair completed; {len(due)} unit(s) restored.")


def transfer_critical_patients():
    usage = usage_snapshot()
    for patient in st.session_state.patients:
        if patient["status"] != "Admitted" or patient["acuity"] != "Critical" or patient["location"] != "ICU" or stay_minutes(patient) < CRITICAL_TRANSFER_MINUTES:
            continue
        # The ICU bed and ventilator are released by changing requirements.
        if available_capacity("Beds", usage) < 1:
            continue
        patient["requirements"] = patient["base_requirements"].copy()
        patient["requirements"].pop("ICU beds", None)
        patient["requirements"].pop("Ventilators", None)
        patient["requirements"]["Beds"] = 1
        patient["location"] = "Normal bed"
        usage = usage_snapshot()
        log(f"{patient['id']} transferred from ICU to a normal bed.")


def auto_discharge():
    count = 0
    for patient in list(st.session_state.patients):
        if patient["status"] != "Admitted":
            continue
        if patient["acuity"] == "Critical" and patient["location"] != "Normal bed":
            continue
        if stay_minutes(patient) >= DISCHARGE_MINUTES.get(patient["acuity"], 9999):
            release_patient(patient)
            count += 1
    return count


def arrival_interval():
    return max(0.1, float(mode_config()["interval"]))


def add_patient():
    ss = st.session_state
    number = ss.next_patient_number
    ss.next_patient_number += 1
    weights = mode_config()["weights"]
    condition, acuity, urgency, requirements = random.choices(CONDITIONS, weights=weights, k=1)[0]
    ss.patients.append(patient_record(f"P-{number:03d}", f"Simulated patient {number}", condition, acuity, urgency, requirements, ss.clock))
    log(f"New {acuity.lower()} patient arrived: P-{number:03d} ({condition}).")


def simulate_failure():
    candidates = [name for name, resource in st.session_state.resources.items() if name != "Beds" and mode_capacity(name) - failed_units(name) > 0]
    if not candidates:
        return
    name = random.choice(candidates)
    minutes = random.randint(60, 180) if name == "ICU beds" else random.randint(30, 120)
    st.session_state.resources[name]["repairs"].append(st.session_state.clock + timedelta(minutes=minutes))
    log(f"Unexpected failure: one {name} unit is down; repair due in {minutes} simulated minute(s).")


def prune_discharged():
    discharged = [p for p in st.session_state.patients if p["status"] == "Discharged"]
    excess = len(discharged) - MAX_DISCHARGED_KEPT
    if excess > 0:
        drop = {p["id"] for p in sorted(discharged, key=lambda p: p["discharged_at"])[:excess]}
        st.session_state.patients = [p for p in st.session_state.patients if p["id"] not in drop]


def record_metrics():
    ss = st.session_state
    waiting = [p for p in ss.patients if p["status"] == "Waiting"]
    usage = usage_snapshot()
    hours = max((ss.clock - ss.sim_start).total_seconds() / 3600, 1 / 60)
    entry = {
        "time": ss.clock,
        "Average waiting time": sum(p["queue_wait_seconds"] for p in waiting) / len(waiting) / 60 if waiting else 0,
        "Maximum waiting time": max((p["queue_wait_seconds"] for p in waiting), default=0) / 60,
        "Critical patients waiting": sum(p["acuity"] == "Critical" for p in waiting),
        "Patients treated": ss.treated_total,
        "Throughput (patients/hr)": ss.treated_total / hours,
    }
    for name in RESOURCE_NAMES:
        entry[f"Utilization: {name}"] = utilization(name, usage)
    ss.metrics_history.append(entry)
    del ss.metrics_history[:-MAX_HISTORY]


def advance_simulation(simulated_seconds):
    ss = st.session_state
    ss.sim_remainder += simulated_seconds
    steps = int(ss.sim_remainder // STEP_SECONDS)
    if steps <= 0:
        return
    ss.sim_remainder -= steps * STEP_SECONDS
    for _ in range(steps):
        ss.clock += timedelta(seconds=STEP_SECONDS)
        for patient in ss.patients:
            if patient["status"] == "Waiting":
                patient["queue_wait_seconds"] += STEP_SECONDS
                patient["reason"] = "Waiting in queue"
        ss.arrival_accumulator += STEP_SECONDS / 60
        while ss.arrival_accumulator >= arrival_interval():
            ss.arrival_accumulator -= arrival_interval()
            add_patient()
        repair_resources()
        transfer_critical_patients()
        auto_discharge()
        allocate_patients()
        record_metrics()
    prune_discharged()
    ss.ticks += steps


def update_from_real_clock():
    ss = st.session_state
    now = time.monotonic()
    elapsed = max(0.0, min(now - ss.last_wall_time, 5.0))
    ss.last_wall_time = now
    if not ss.paused and elapsed > 0:
        advance_simulation(elapsed * ss.speed)


def strategy_snapshot(strategy):
    patients = deepcopy(st.session_state.patients)
    resources = deepcopy(st.session_state.resources)
    waiting = [p for p in patients if p["status"] == "Waiting"]
    base_totals = st.session_state.base_resource_totals.copy()
    config = mode_config()
    scenario_totals = {name: max(0, int(round(base_totals[name] * (1 - config["factors"].get(name, 0.0))))) for name in RESOURCE_NAMES}
    for name in RESOURCE_NAMES:
        resources[name]["total"] = scenario_totals[name]

    def local_effective(name):
        return max(0, resources[name]["total"] - len(resources[name]["repairs"]))

    def local_usage():
        usage = dict.fromkeys(RESOURCE_NAMES, 0)
        for p in patients:
            if p["status"] == "Admitted":
                for name, amount in p["requirements"].items():
                    usage[name] += amount
        return usage

    def score(patient):
        return patient["urgency"] * 10 if strategy == "Urgency Only" else priority_breakdown(patient)["Final Priority"]

    usage = local_usage()
    admitted_now = 0
    for patient in sorted(waiting, key=score, reverse=True):
        if any(local_effective(name) - usage.get(name, 0) < needed for name, needed in patient["requirements"].items()):
            continue
        for name, needed in patient["requirements"].items():
            usage[name] += needed
        patient["status"] = "Admitted"
        admitted_now += 1

    remaining = [p for p in patients if p["status"] == "Waiting"]
    waits = [p["queue_wait_seconds"] / 60 for p in remaining]
    critical_wait = sum(p["queue_wait_seconds"] / 60 for p in remaining if p["acuity"] == "Critical")
    utils = [usage.get(name, 0) / local_effective(name) * 100 if local_effective(name) else 0 for name in RESOURCE_NAMES]
    return {
        "Average Wait": sum(waits) / len(waits) if waits else 0.0,
        "Maximum Wait": max(waits, default=0.0),
        "Critical Wait": critical_wait,
        "Patients Treated": admitted_now,
        "Throughput": admitted_now,
        "Average Resource Utilization": sum(utils) / len(utils),
    }


def skip_minutes(minutes):
    advance_simulation(minutes * 60)
    st.session_state.last_wall_time = time.monotonic()


def reset_simulation():
    initialize()


def reset_arrival_accumulator():
    st.session_state.arrival_accumulator = 0.0


ensure_state()
st.sidebar.title("⚙️ Simulation controls")
st.sidebar.selectbox("Operating mode", list(MODE_LABELS), key="mode", format_func=lambda x: f"{x} — {MODE_LABELS[x]}", on_change=reset_arrival_accumulator)
if st.session_state.mode == "Custom":
    st.sidebar.number_input("Minutes between arrivals", 0.5, 240.0, key="custom_interval", step=0.5)
    st.sidebar.caption("Custom arrival mix and capacity reductions")
    for acuity in ("Critical", "Urgent", "Routine"):
        st.session_state.custom_weights[acuity] = st.sidebar.slider(f"{acuity} arrival weight", 1, 10, int(st.session_state.custom_weights[acuity]))
    for name in RESOURCE_NAMES:
        st.session_state.custom_factors[name] = st.sidebar.slider(f"{name} reduction", 0.0, 0.8, float(st.session_state.custom_factors[name]), 0.05)
st.sidebar.number_input("Simulation speed", 0.1, 500.0, key="speed", step=0.1, format="%.1f")
st.sidebar.checkbox("⏸ Pause simulation", key="paused")
st.sidebar.caption("Queue wait grows while a patient is waiting; admission occurs as soon as required resources are free.")
for column, minutes in zip(st.sidebar.columns(5), (1, 5, 10, 15, 20)):
    column.button(f"+{minutes}m", key=f"skip_{minutes}", on_click=skip_minutes, args=(minutes,), use_container_width=True)
st.sidebar.button("⚡ Simulate unexpected failure", on_click=simulate_failure, use_container_width=True)
st.sidebar.button("🔄 Reset simulation", on_click=reset_simulation, use_container_width=True)

if st.session_state.mode != st.session_state.last_mode:
    st.session_state.last_mode = st.session_state.mode
    st.session_state.arrival_accumulator = 0.0
    log(f"Scenario changed to {st.session_state.mode}; temporary conditions updated.")

apply_mode = mode_config()
st_autorefresh(interval=1000, key="medflow_clock")
update_from_real_clock()

st.title("🏥 MedFlow")
st.caption("Hospital management and resource-allocation simulation — educational model, not for clinical use")
paused_note = " · ⏸ **paused**" if st.session_state.paused else ""
st.info(f"Simulation time: **{st.session_state.clock:%Y-%m-%d %H:%M:%S}** · Mode: **{st.session_state.mode}** · Speed: **{st.session_state.speed:.1f}×**{paused_note}")
st.caption("Scenario effects: " + " | ".join(f"{key}: {value}" for key, value in scenario_summary().items()))
st.caption("Priority formula: P = Urgency×10 + Waiting Time + Resource Criticality×2")

usage = usage_snapshot()
st.subheader("Hospital resources")
resource_columns = st.columns(3)
for i, name in enumerate(RESOURCE_NAMES):
    resource = st.session_state.resources[name]
    with resource_columns[i % 3]:
        base_total = st.number_input(f"{name} — total", 0, 200, int(st.session_state.base_resource_totals[name]), key=f"total_{name}")
        if base_total != st.session_state.base_resource_totals[name]:
            st.session_state.base_resource_totals[name] = base_total
            log(f"Base capacity for {name} changed to {base_total}.")
        effective = effective_capacity(name)
        st.metric("Available", available_capacity(name, usage), delta=f"{usage.get(name, 0)} used", delta_color="off")
        st.caption(f"Effective: {effective} · Utilization: {utilization(name, usage):.0f}%")
        if resource["repairs"]:
            st.caption(f"⚠️ Failed units: {len(resource['repairs'])} · next repair {min(resource['repairs']):%H:%M}")

patients = st.session_state.patients
waiting = [p for p in patients if p["status"] == "Waiting"]
admitted = [p for p in patients if p["status"] == "Admitted"]
discharged = [p for p in patients if p["status"] == "Discharged"]
avg_wait = sum(p["queue_wait_seconds"] for p in waiting) / len(waiting) / 60 if waiting else 0.0
max_wait = max((p["queue_wait_seconds"] for p in waiting), default=0.0) / 60
m1, m2, m3, m4 = st.columns(4)
m1.metric("Waiting", len(waiting))
m2.metric("Currently admitted", len(admitted))
m3.metric("Average waiting time", f"{avg_wait:.1f} min")
m4.metric("Patients treated", st.session_state.treated_total)

st.subheader("Patient flow")
tab_wait, tab_admit, tab_dis, tab_res, tab_stats, tab_compare, tab_log = st.tabs(["🕒 Waiting list", "🛏️ Currently admitted", "✅ Discharged", "📊 Resource utilization", "📈 Statistics", "⚖️ Strategy Comparison", "📋 Event log"])
with tab_wait:
    rows = []
    for i, patient in enumerate(sorted(waiting, key=patient_priority, reverse=True)[:200], 1):
        breakdown = priority_breakdown(patient)
        rows.append({"#": i, "ID": patient["id"], "Patient": patient["name"], "Condition": patient["condition"], "Acuity": patient["acuity"], "Priority P": f"{breakdown['Final Priority']:.1f}", "Urgency": f"{breakdown['Urgency contribution']:.0f}", "Wait": f"{breakdown['Waiting contribution']:.1f}", "Resource": f"{breakdown['Resource contribution']:.0f}", "Queue wait": f"{patient['queue_wait_seconds'] / 60:.1f} min", "Reason": patient["reason"]})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.success("No patients are waiting.")
with tab_admit:
    if admitted:
        ordered = sorted(admitted, key=stay_minutes, reverse=True)
        labels = {p["id"]: f"{p['name']} ({p['id']}) — {p['location']} — {stay_minutes(p)} min" for p in ordered}
        selected = st.selectbox("Why was this patient selected?", list(labels), format_func=labels.get)
        selected_patient = next(p for p in ordered if p["id"] == selected)
        breakdown = priority_breakdown(selected_patient)
        st.write(f"**{selected_patient['name']} — {selected_patient['condition']}**")
        st.write(f"Urgency score: {selected_patient['urgency']} · Waiting time: {selected_patient['queue_wait_seconds'] / 60:.1f} min · Resource criticality: {resource_criticality(selected_patient)}")
        st.write(f"Urgency: {breakdown['Urgency contribution']:.1f} + Wait: {breakdown['Waiting contribution']:.1f} + Resource: {breakdown['Resource contribution']:.1f} = **{breakdown['Final Priority']:.1f}**")
        selected_ids = st.multiselect("Patients to discharge", list(labels), format_func=labels.get)
        if st.button("✅ Discharge selected patient(s)", type="primary", disabled=not selected_ids):
            for pid in selected_ids:
                discharge_patient(pid)
            allocate_patients()
            st.rerun()
        st.dataframe(pd.DataFrame([{ "ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Acuity": p["acuity"], "Location": p["location"], "Stay": f"{stay_minutes(p)} min", "Priority P": f"{priority_breakdown(p)['Final Priority']:.1f}" } for p in ordered]), use_container_width=True, hide_index=True)
    else:
        st.warning("No patients are currently admitted.")
with tab_dis:
    recent = sorted((p for p in discharged if p["discharged_at"]), key=lambda p: p["discharged_at"], reverse=True)[:200]
    if recent:
        st.dataframe(pd.DataFrame([{ "ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Acuity": p["acuity"], "Queue wait": f"{p['queue_wait_seconds'] / 60:.1f} min", "Discharged": p["discharged_at"].strftime("%H:%M:%S") } for p in recent]), use_container_width=True, hide_index=True)
    else:
        st.info("No patients have been discharged yet.")
with tab_res:
    st.dataframe(pd.DataFrame([{ "Resource": name, "Base total": st.session_state.base_resource_totals[name], "Mode capacity": mode_capacity(name), "Effective": effective_capacity(name), "Failed": failed_units(name), "Used": usage.get(name, 0), "Available": available_capacity(name, usage), "Utilization": f"{utilization(name, usage):.0f}%" } for name in RESOURCE_NAMES]), use_container_width=True, hide_index=True)
with tab_stats:
    history = pd.DataFrame(st.session_state.metrics_history)
    critical_waiting = sum(p["acuity"] == "Critical" for p in waiting)
    hours = max((st.session_state.clock - st.session_state.sim_start).total_seconds() / 3600, 1 / 60)
    overall_util = sum(utilization(name, usage) for name in RESOURCE_NAMES) / len(RESOURCE_NAMES)
    s1, s2, s3 = st.columns(3)
    s1.metric("Average waiting time", f"{avg_wait:.1f} min")
    s2.metric("Maximum waiting time", f"{max_wait:.1f} min")
    s3.metric("Critical patients waiting", critical_waiting)
    s4, s5, s6 = st.columns(3)
    s4.metric("Patients treated", st.session_state.treated_total)
    s5.metric("Throughput", f"{st.session_state.treated_total / hours:.1f} patients/hr")
    s6.metric("Resource utilization", f"{overall_util:.1f}%")
    if not history.empty:
        st.line_chart(history.set_index("time")[["Average waiting time", "Maximum waiting time"]], use_container_width=True)
        util_cols = [c for c in history.columns if c.startswith("Utilization:")]
        st.line_chart(history.set_index("time")[util_cols], use_container_width=True)
        st.line_chart(history.set_index("time")[["Patients treated", "Throughput (patients/hr)"]], use_container_width=True)
    else:
        st.info("Statistics will appear after the simulation advances.")
with tab_compare:
    def snapshot(strategy):
        simulated = deepcopy(st.session_state.patients)
        waiting_copy = [p for p in simulated if p["status"] == "Waiting"]
        usage_copy = usage_snapshot(simulated)
        order = sorted(waiting_copy, key=(lambda p: p["urgency"] * 10) if strategy == "Urgency Only" else patient_priority, reverse=True)
        admitted_count = 0
        for patient in order:
            if any(effective_capacity(name) - usage_copy.get(name, 0) < needed for name, needed in patient["requirements"].items()):
                continue
            for name, needed in patient["requirements"].items():
                usage_copy[name] += needed
            patient["status"] = "Admitted"
            admitted_count += 1
        remaining = [p for p in simulated if p["status"] == "Waiting"]
        waits = [p["queue_wait_seconds"] / 60 for p in remaining]
        utils = [utilization(name, usage_copy) for name in RESOURCE_NAMES]
        return {"Average Wait": sum(waits) / len(waits) if waits else 0, "Maximum Wait": max(waits, default=0), "Critical Wait": sum(p["queue_wait_seconds"] / 60 for p in remaining if p["acuity"] == "Critical"), "Patients Treated": admitted_count, "Throughput": admitted_count, "Average Resource Utilization": sum(utils) / len(utils)}
    comparison = {"Urgency Only": snapshot("Urgency Only"), "MEDFLOW": snapshot("MEDFLOW")}
    comparison_df = pd.DataFrame([{ "Metric": metric, "Urgency Only": comparison["Urgency Only"][metric], "MEDFLOW": comparison["MEDFLOW"][metric] } for metric in comparison["MEDFLOW"]])
    st.dataframe(comparison_df, use_container_width=True, hide_index=True)
    st.bar_chart(comparison_df.set_index("Metric"), use_container_width=True)
    st.caption("Both strategies use the same current patient/resource snapshot; only the scheduling rule changes.")
with tab_log:
    for event in st.session_state.event_log[:12]:
        st.write(f"• {event}")

shortages = [f"{failed_units(name)} {name} failed" for name in RESOURCE_NAMES if failed_units(name)]
if any(p["acuity"] == "Critical" for p in waiting):
    shortages.append("critical patient waiting")
if shortages:
    st.divider()
    st.warning("⚠️ Silent alarm — " + "; ".join(shortages) + ". Review staffing or resource allocation.", icon="⚠️")
