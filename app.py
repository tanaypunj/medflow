import random
import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="MedFlow", page_icon="🏥", layout="wide")

RESOURCE_DEFAULTS = {"Beds": 30, "ICU beds": 6, "Doctors": 8, "Nurses": 16, "Operating rooms": 3, "Ventilators": 5}
MODE_LABELS = {"Normal": "Routine operations", "Emergency surge": "Higher emergency arrivals and acuity", "Staff shortage": "Reduced clinical staffing", "Resource shortage": "Reduced beds and equipment", "Disaster": "Severe surge with multiple constraints", "Custom": "Use configured resources"}
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
DISCHARGE_PRIORITY = {"Routine": 0, "Urgent": 1, "Critical": 2}
CRITICAL_TRANSFER_MINUTES = 120


def patient_record(pid, name, condition, acuity, priority, requirements, arrival, initial_wait=0):
    return {
        "id": pid, "name": name, "condition": condition, "acuity": acuity,
        "priority": priority, "arrival": arrival, "requirements": requirements.copy(),
        "base_requirements": requirements.copy(), "status": "Waiting", "location": "Waiting list",
        "admitted_at": None, "discharged_at": None, "waiting_seconds": initial_wait,
        "reason": "Not yet assessed",
    }


def seed_patients():
    now = datetime.now().replace(microsecond=0)
    names = ["A. Patel", "B. Williams", "C. Garcia", "D. Chen", "E. Smith", "F. Okafor", "G. Jones", "H. Khan"]
    return [patient_record(f"P-{i + 1:03d}", name, *CONDITIONS[i % len(CONDITIONS)], now - timedelta(minutes=(i + 1) * 12), (i + 1) * 12 * 60) for i, name in enumerate(names)]


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


def ensure_state():
    if "patients" not in st.session_state:
        initialize()
    for patient in st.session_state.patients:
        patient.setdefault("waiting_seconds", patient.get("waiting_minutes", 0) * 60)
        patient.setdefault("location", "Waiting list")
        patient.setdefault("base_requirements", patient.get("requirements", {}).copy())
        patient.setdefault("discharged_at", None)
    for resource in st.session_state.resources.values():
        resource.setdefault("repair_at", None)
    st.session_state.setdefault("speed", 1.0)
    st.session_state.setdefault("arrival_accumulator", 0.0)
    st.session_state.setdefault("last_wall_time", time.monotonic())


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


def doctor_wait_factor(patient):
    """Return the fraction of waiting progress supported by available doctors."""
    required = max(1, patient["requirements"].get("Doctors", 1))
    available = available_capacity("Doctors")
    return min(1.0, available / required) if available > 0 else 0.0


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
    waiting = sorted((p for p in st.session_state.patients if p["status"] == "Waiting"), key=lambda p: (p["priority"], p["arrival"]))
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
    ordered = sorted((p for p in st.session_state.patients if p["status"] == "Admitted"), key=lambda p: (DISCHARGE_PRIORITY.get(p["acuity"], 99), -stay_minutes(p)))
    count = 0
    for patient in ordered:
        if patient["acuity"] == "Critical" and patient["location"] != "Normal bed":
            continue
        if stay_minutes(patient) >= DISCHARGE_MINUTES.get(patient["acuity"], 9999) and discharge_patient(patient["id"]):
            count += 1
    return count


def add_patient():
    number = len(st.session_state.patients) + 1
    condition, acuity, priority, requirements = random.choice(CONDITIONS)
    patient = patient_record(f"P-{number:03d}", f"Simulated patient {number}", condition, acuity, priority, requirements, st.session_state.clock)
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
        message = f"Unexpected ICU failure; repair due in {hours} simulated hour(s)."
    else:
        message = f"Unexpected failure: one {name} became unavailable."
    st.session_state.event_log.insert(0, message)


def advance_simulation(simulated_seconds):
    """Advance in 30-second steps. Waiting progress is reduced when doctors are unavailable."""
    steps = max(1, int(simulated_seconds // 30))
    for _ in range(steps):
        st.session_state.clock += timedelta(seconds=30)
        for patient in st.session_state.patients:
            if patient["status"] == "Waiting":
                patient["waiting_seconds"] = max(0, patient["waiting_seconds"] - 30 * doctor_wait_factor(patient))
                doctor_message = doctor_wait_message(patient)
                if doctor_message:
                    patient["reason"] = doctor_message
        st.session_state.arrival_accumulator += 30 / 60
        interval = ARRIVAL_INTERVALS[st.session_state.mode]
        while st.session_state.arrival_accumulator >= interval:
            st.session_state.arrival_accumulator -= interval
            add_patient()
        repair_resources()
        transfer_critical_patients()
        auto_discharge()
        allocate_patients()
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

st.session_state.speed = st.sidebar.number_input("Simulation speed", min_value=0.1, max_value=500.0, value=float(st.session_state.speed), step=0.1, format="%.1fx", help="Simulated time multiplier. Maximum 500×.")
st.sidebar.caption("Patient statuses are evaluated every 30 simulated seconds. Patients arrive automatically based on the selected mode.")

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
st.caption("Normal beds never fail. ICU failures are repaired in 1–3 simulated hours.")
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
m3.metric("Average remaining wait", f"{(sum(p['waiting_seconds'] for p in waiting) / len(waiting) / 60 if waiting else 0):.1f} min")
m4.metric("Discharged", len(discharged))

st.subheader("Patient flow")
tab_wait, tab_admit, tab_dis, tab_res, tab_log = st.tabs(["🕒 Waiting list", "🛏️ Currently admitted", "✅ Discharged", "📊 Resource utilization", "📋 Event log"])
with tab_wait:
    rows = [{"#": i, "ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Acuity": p["acuity"], "Remaining wait": f"{p['waiting_seconds'] / 60:.1f} min", "Doctor availability": f"{available_capacity('Doctors')}/{p['requirements'].get('Doctors', 1)}", "Why waiting": p["reason"]} for i, p in enumerate(sorted(waiting, key=lambda p: (p["priority"], p["arrival"])), 1)]
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.success("No patients are waiting.")
with tab_admit:
    if admitted:
        ordered = sorted(admitted, key=lambda p: (DISCHARGE_PRIORITY.get(p["acuity"], 99), -stay_minutes(p)))
        selected = st.multiselect("Patients to discharge", [p["id"] for p in ordered], format_func=lambda pid: next(f"{p['name']} ({pid}) — {p['location']} — {stay_minutes(p)} min" for p in ordered if p["id"] == pid), key="discharge_selection")
        if st.button("✅ Discharge selected patient(s)", type="primary", disabled=not selected):
            for pid in selected:
                discharge_patient(pid)
            allocate_patients()
            st.rerun()
        admitted_rows = [{"ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Acuity": p["acuity"], "Location": p["location"], "Stay": f"{stay_minutes(p)} min", "Resources": ", ".join(f"{k}: {v}" for k, v in p["requirements"].items())} for p in ordered]
        st.dataframe(pd.DataFrame(admitted_rows), use_container_width=True, hide_index=True)
    else:
        st.warning("No patients are currently admitted.")
with tab_dis:
    discharged_rows = [{"ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Acuity": p["acuity"], "Discharged": p["discharged_at"].strftime("%H:%M:%S")} for p in discharged if p.get("discharged_at") is not None]
    if discharged_rows:
        st.dataframe(pd.DataFrame(discharged_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No patients have been discharged yet.")
with tab_res:
    resource_rows = [{"Resource": name, "Total": resource["total"], "Used": current_usage(name), "Failed": resource["failed"], "Available": available_capacity(name), "Utilization": f"{(current_usage(name) / resource['total'] * 100) if resource['total'] else 0:.0f}%"} for name, resource in st.session_state.resources.items()]
    st.dataframe(pd.DataFrame(resource_rows), use_container_width=True, hide_index=True)
with tab_log:
    for event in st.session_state.event_log[:12]:
        st.write(f"• {event}")

shortages = [f"{resource['failed']} {name} failed" for name, resource in st.session_state.resources.items() if resource["failed"]]
if any(p["acuity"] == "Critical" and p["status"] == "Waiting" for p in waiting):
    shortages.append("critical patient waiting")
if shortages:
    st.divider()
    st.warning("⚠️ Silent alarm — " + "; ".join(shortages) + ". Review staffing or resource allocation.", icon="⚠️")
