import random
import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="MedFlow", page_icon="🏥", layout="wide")

RESOURCE_DEFAULTS = {"Beds": 30, "ICU beds": 6, "Doctors": 8, "Nurses": 16, "Operating rooms": 3, "Ventilators": 5}
MODE_LABELS = {
    "Normal": "Routine operations",
    "Emergency surge": "Higher emergency arrivals and acuity",
    "Staff shortage": "Reduced clinical staffing",
    "Resource shortage": "Reduced beds and equipment capacity",
    "Disaster": "Mass-casualty event",
    "Custom": "Custom arrival rate",
}
ARRIVAL_INTERVALS = {"Normal": 30, "Emergency surge": 5, "Staff shortage": 15, "Resource shortage": 12, "Disaster": 2, "Custom": 20}
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


def patient_record(pid, name, condition, acuity, urgency, requirements, arrival, initial_wait=0):
    return {
        "id": pid,
        "name": name,
        "condition": condition,
        "acuity": acuity,
        "urgency": urgency,
        "arrival": arrival,
        "requirements": requirements.copy(),
        "base_requirements": requirements.copy(),
        "status": "Waiting",
        "location": "Waiting list",
        "admitted_at": None,
        "discharged_at": None,
        "waiting_seconds": float(initial_wait),
        "waiting_elapsed_seconds": float(initial_wait),
        "reason": "Not yet assessed",
    }


def seed_patients():
    now = datetime.now().replace(microsecond=0)
    names = ["A. Patel", "B. Williams", "C. Garcia", "D. Chen", "E. Smith", "F. Okafor", "G. Jones", "H. Khan"]
    patients = []
    for i, name in enumerate(names):
        condition, acuity, urgency, requirements = CONDITIONS[i % len(CONDITIONS)]
        patient = patient_record(
            f"P-{i + 1:03d}",
            name,
            condition,
            acuity,
            urgency,
            requirements,
            now - timedelta(minutes=(i + 1) * 12),
            0,
        )
        patients.append(patient)
    return patients


def initialize():
    st.session_state.clock = datetime.now().replace(microsecond=0)
    st.session_state.patients = seed_patients()
    st.session_state.resources = {name: {"total": total, "failed": 0, "repair_at": None} for name, total in RESOURCE_DEFAULTS.items()}
    st.session_state.mode = "Normal"
    st.session_state.speed = 1.0
    st.session_state.event_log = ["Simulation initialized"]
    st.session_state.ticks = 0
    st.session_state.arrival_accumulator = 0.0
    st.session_state.last_wall_time = time.monotonic()
    st.session_state.metrics_history = []


def ensure_state():
    if "patients" not in st.session_state:
        initialize()
    for patient in st.session_state.patients:
        patient.setdefault("waiting_seconds", patient.get("waiting_minutes", 0) * 60)
        patient.setdefault("waiting_elapsed_seconds", patient.get("waiting_seconds", 0.0))
        patient.setdefault("location", "Waiting list")
        patient.setdefault("base_requirements", patient.get("requirements", {}).copy())
        patient.setdefault("discharged_at", None)
        patient.setdefault("urgency", {"Routine": 2, "Urgent": 3, "Critical": 4}.get(patient.get("acuity"), 2))
    for resource in st.session_state.resources.values():
        resource.setdefault("repair_at", None)
    st.session_state.setdefault("speed", 1.0)
    st.session_state.setdefault("arrival_accumulator", 0.0)
    st.session_state.setdefault("last_wall_time", time.monotonic())
    st.session_state.setdefault("metrics_history", [])

    # Actual queue wait grows while waiting. Admission depends on resource availability, not an arbitrary countdown.
    allocate_patients()


def effective_capacity(name):
    resource = st.session_state.resources[name]
    return max(0, resource["total"] - resource["failed"])


def current_usage(name):
    return sum(p["requirements"].get(name, 0) for p in st.session_state.patients if p["status"] == "Admitted")


def available_capacity(name):
    return max(0, effective_capacity(name) - current_usage(name))


def stay_minutes(patient):
    if patient["admitted_at"] is None:
        return 0
    end = patient["discharged_at"] or st.session_state.clock
    return max(0, int((end - patient["admitted_at"]).total_seconds() // 60))


def resource_criticality(patient):
    weights = {"ICU beds": 3, "Ventilators": 3, "Operating rooms": 2, "Doctors": 1, "Nurses": 1, "Beds": 1}
    return sum(weights.get(name, 1) for name, amount in patient["requirements"].items() if amount > 0)


def patient_priority(patient):
    """P = U×10 + W×1 + R×2; W is actual queue wait in minutes."""
    return patient["urgency"] * 10 + (patient["waiting_elapsed_seconds"] / 60) + resource_criticality(patient) * 2


def doctor_wait_factor(patient):
    required = max(1, patient["requirements"].get("Doctors", 1))
    available = available_capacity("Doctors")
    return min(1.0, available / required) if available > 0 else 0.0


def wait_progress_factor(patient):
    factors = []
    for name, needed in patient["requirements"].items():
        if needed:
            factors.append(min(1.0, available_capacity(name) / needed))
    return min(factors) if factors else 1.0


def doctor_wait_message(patient):
    available = available_capacity("Doctors")
    required = patient["requirements"].get("Doctors", 1)
    if available < required:
        return f"Waiting for Doctors ({available}/{required} available)"
    return ""


def can_admit(patient):
    return [f"{name} ({available_capacity(name)}/{needed})" for name, needed in patient["requirements"].items() if available_capacity(name) < needed]


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
        count += 1
    return count


def discharge_patient(pid):
    for patient in st.session_state.patients:
        if patient["id"] == pid and patient["status"] == "Admitted":
            old_location = patient["location"]
            patient["status"] = "Discharged"
            patient["discharged_at"] = st.session_state.clock
            patient["location"] = "Discharged"
            st.session_state.event_log.insert(0, f"{patient['id']} ({patient['name']}) discharged from {old_location}; resources released.")
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
    condition, acuity, urgency, requirements = random.choice(CONDITIONS)
    patient = patient_record(
        f"P-{number:03d}",
        f"Simulated patient {number}",
        condition,
        acuity,
        urgency,
        requirements,
        st.session_state.clock,
    )
    st.session_state.patients.append(patient)
    st.session_state.event_log.insert(0, f"New {acuity.lower()} patient arrived automatically: {patient['id']} ({patient['condition']}).")


def simulate_failure():
    candidates = [name for name, resource in st.session_state.resources.items() if name != "Beds" and resource["total"] - resource["failed"] > 0]
    if not candidates:
        return
    name = random.choice(candidates)
    resource = st.session_state.resources[name]
    resource["failed"] += 1
    if name == "ICU beds":
        hours = random.randint(1, 3)
        resource["repair_at"] = st.session_state.clock + timedelta(hours=hours)
        message = f"Unexpected ICU failure; repair due in {hours} simulated hour(s). Queue wait grows until the ICU is repaired."
    else:
        message = f"Unexpected failure: one {name} became unavailable. Queue wait grows until recovery."
    st.session_state.event_log.insert(0, message)


def record_metrics():
    waiting = [p for p in st.session_state.patients if p["status"] == "Waiting"]
    admitted = [p for p in st.session_state.patients if p["status"] == "Admitted"]
    treated = [p for p in st.session_state.patients if p["status"] == "Discharged"]
    utilization = {name: (current_usage(name) / resource["total"] * 100 if resource["total"] else 0) for name, resource in st.session_state.resources.items()}
    entry = {
        "time": st.session_state.clock,
        "Average waiting time": sum(p["waiting_elapsed_seconds"] for p in waiting) / len(waiting) / 60 if waiting else 0,
        "Maximum waiting time": max((p["waiting_elapsed_seconds"] for p in waiting), default=0) / 60,
        "Critical patients waiting": sum(p["acuity"] == "Critical" for p in waiting),
        "Patients treated": len(treated),
        "Throughput": len(treated),
        "Admitted": len(admitted),
    }
    for name, value in utilization.items():
        entry[f"Utilization: {name}"] = value
    st.session_state.metrics_history.append(entry)
    st.session_state.metrics_history = st.session_state.metrics_history[-300:]


def advance_simulation(simulated_seconds):
    """Advance in 30-second steps. Waiting time strictly increases while a patient is queued."""
    steps = max(1, int(simulated_seconds // 30))
    for _ in range(steps):
        st.session_state.clock += timedelta(seconds=30)
        for patient in st.session_state.patients:
            if patient["status"] == "Waiting":
                patient["waiting_elapsed_seconds"] += 30
                patient["waiting_seconds"] = patient["waiting_elapsed_seconds"]
                patient["reason"] = doctor_wait_message(patient) or "Waiting in queue"
        st.session_state.arrival_accumulator += 30 / 60
        interval = ARRIVAL_INTERVALS[st.session_state.mode]
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


ensure_state()
st_autorefresh(interval=1000, key="medflow_clock")
update_from_real_clock()

st.sidebar.title("⚙️ Simulation controls")
mode = st.sidebar.selectbox("Operating mode", list(MODE_LABELS), index=list(MODE_LABELS).index(st.session_state.mode), format_func=lambda x: f"{x} — {MODE_LABELS[x]}")
if mode != st.session_state.mode:
    st.session_state.mode = mode
    st.session_state.arrival_accumulator = 0.0

st.session_state.speed = st.sidebar.number_input(
    "Simulation speed",
    min_value=0.1,
    max_value=500.0,
    value=float(st.session_state.speed),
    step=0.1,
    format="%.1f",
    help="Simulated time multiplier. Maximum 500×.",
)
st.sidebar.caption("Queue wait grows while a patient is waiting; admission occurs as soon as the required resources are free.")

skip_columns = st.sidebar.columns(5)
for column, minutes in zip(skip_columns, (1, 5, 10, 15, 20)):
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

st.title("🏥 MedFlow")
st.caption("Hospital management and resource-allocation simulation")
st.info(f"Simulation time: **{st.session_state.clock:%Y-%m-%d %H:%M:%S}** · Mode: **{st.session_state.mode}** · Speed: **{st.session_state.speed:.1f}×")

st.subheader("Hospital resources")
st.caption("Priority formula: P = U×10 + W×1 + R×2, where W is elapsed queue wait in minutes.")
resource_columns = st.columns(3)
for i, name in enumerate(st.session_state.resources):
    resource = st.session_state.resources[name]
    with resource_columns[i % 3]:
        resource["total"] = st.number_input(f"{name} — total", min_value=0, max_value=200, value=int(resource["total"]), key=f"total_{name}")
        st.metric("Available", available_capacity(name), delta=f"{current_usage(name)} used")
        if resource["failed"]:
            due = f" · repair due {resource['repair_at']:%H:%M}" if resource["repair_at"] else ""
            st.caption(f"⚠️ Failed units: {resource['failed']}{due}")

waiting = [p for p in st.session_state.patients if p["status"] == "Waiting"]
admitted = [p for p in st.session_state.patients if p["status"] == "Admitted"]
discharged = [p for p in st.session_state.patients if p["status"] == "Discharged"]
m1, m2, m3, m4 = st.columns(4)
m1.metric("Waiting", len(waiting))
m2.metric("Currently admitted", len(admitted))
m3.metric("Average waiting time", f"{(sum(p['waiting_elapsed_seconds'] for p in waiting) / len(waiting) / 60 if waiting else 0):.1f} min")
m4.metric("Patients treated", len(discharged))

st.subheader("Patient flow")
tab_wait, tab_admit, tab_dis, tab_res, tab_stats, tab_log = st.tabs(["🕒 Waiting list", "🛏️ Currently admitted", "✅ Discharged", "📊 Resource utilization", "📈 Statistics", "📋 Event log"])
with tab_wait:
    rows = [{
        "#": i,
        "ID": p["id"],
        "Patient": p["name"],
        "Condition": p["condition"],
        "Acuity": p["acuity"],
        "Priority P": f"{patient_priority(p):.1f}",
        "Queue wait": f"{p['waiting_seconds'] / 60:.1f} min",
        "Reason": p["reason"],
    } for i, p in enumerate(sorted(waiting, key=patient_priority, reverse=True), start=1)]
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.success("No patients are waiting.")
with tab_admit:
    if admitted:
        ordered = sorted(admitted, key=patient_priority, reverse=True)
        selected = st.multiselect(
            "Patients to discharge",
            [p["id"] for p in ordered],
            format_func=lambda pid: next(f"{p['name']} ({pid}) — {p['location']} — {stay_minutes(p)} min" for p in ordered if p["id"] == pid),
        )
        if st.button("✅ Discharge selected patient(s)", type="primary", disabled=not selected):
            for pid in selected:
                discharge_patient(pid)
            allocate_patients()
            st.rerun()
        admitted_rows = [{
            "ID": p["id"],
            "Patient": p["name"],
            "Condition": p["condition"],
            "Acuity": p["acuity"],
            "Location": p["location"],
            "Stay": f"{stay_minutes(p)} min",
            "Resources": ", ".join(f"{name}:{qty}" for name, qty in p["requirements"].items()),
        } for p in ordered]
        st.dataframe(pd.DataFrame(admitted_rows), use_container_width=True, hide_index=True)
    else:
        st.warning("No patients are currently admitted.")
with tab_dis:
    discharged_rows = [{
        "ID": p["id"],
        "Patient": p["name"],
        "Condition": p["condition"],
        "Acuity": p["acuity"],
        "Queue wait": f"{p['waiting_elapsed_seconds'] / 60:.1f} min",
        "Discharged": p["discharged_at"].strftime("%H:%M:%S"),
    } for p in discharged if p.get("discharged_at")]
    if discharged_rows:
        st.dataframe(pd.DataFrame(discharged_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No patients have been discharged yet.")
with tab_res:
    resource_rows = [{
        "Resource": name,
        "Total": resource["total"],
        "Used": current_usage(name),
        "Failed": resource["failed"],
        "Available": available_capacity(name),
        "Utilization": f"{(current_usage(name) / resource['total'] * 100 if resource['total'] else 0):.0f}%",
    } for name, resource in st.session_state.resources.items()]
    st.dataframe(pd.DataFrame(resource_rows), use_container_width=True, hide_index=True)
with tab_stats:
    history = pd.DataFrame(st.session_state.metrics_history)
    current_avg = sum(p["waiting_elapsed_seconds"] for p in waiting) / len(waiting) / 60 if waiting else 0
    current_max = max((p["waiting_elapsed_seconds"] for p in waiting), default=0) / 60
    critical_waiting = sum(p["acuity"] == "Critical" for p in waiting)
    throughput = len(discharged)
    s1, s2, s3 = st.columns(3)
    s1.metric("Average waiting time", f"{current_avg:.1f} min")
    s2.metric("Maximum waiting time", f"{current_max:.1f} min")
    s3.metric("Critical patients waiting", critical_waiting)
    s4, s5, s6 = st.columns(3)
    s4.metric("Patients treated", len(discharged))
    s5.metric("Throughput", f"{throughput} patients")
    overall_util = sum(current_usage(n) / r["total"] * 100 if r["total"] else 0 for n, r in st.session_state.resources.items()) / len(st.session_state.resources)
    s6.metric("Resource utilization", f"{overall_util:.1f}%")
    if not history.empty:
        chart = history.set_index("time")[["Average waiting time", "Maximum waiting time"]]
        st.line_chart(chart, use_container_width=True)
        utilization_columns = [c for c in history.columns if c.startswith("Utilization:")]
        if utilization_columns:
            st.caption("Resource utilization over simulation time")
            st.line_chart(history.set_index("time")[utilization_columns], use_container_width=True)
        st.caption("Patients treated / throughput over simulation time")
        st.line_chart(history.set_index("time")[["Patients treated", "Throughput"]], use_container_width=True)
    else:
        st.info("Statistics will appear after the simulation advances.")
with tab_log:
    for event in st.session_state.event_log[:12]:
        st.write(f"• {event}")

shortages = [f"{resource['failed']} {name} failed" for name, resource in st.session_state.resources.items() if resource["failed"]]
if any(p["acuity"] == "Critical" and p["status"] == "Waiting" for p in waiting):
    shortages.append("critical patient waiting")
if shortages:
    st.divider()
    st.warning("⚠️ Silent alarm — " + "; ".join(shortages) + ". Review staffing or resource allocation.", icon="⚠️")
