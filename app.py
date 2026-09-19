import random
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st


st.set_page_config(page_title="MedFlow", page_icon="🏥", layout="wide")

RESOURCE_DEFAULTS = {
    "Beds": 30,
    "ICU beds": 6,
    "Doctors": 8,
    "Nurses": 16,
    "Operating rooms": 3,
    "Ventilators": 5,
}

MODE_LABELS = {
    "Normal": "Routine operations",
    "Emergency surge": "Higher emergency arrivals and acuity",
    "Staff shortage": "Reduced clinical staffing",
    "Resource shortage": "Reduced beds and equipment",
    "Disaster": "Severe surge with multiple constraints",
    "Custom": "Use the resource values configured below",
}

CONDITIONS = [
    ("Cardiac event", "Critical", 4, {"Beds": 1, "ICU beds": 1, "Doctors": 1, "Nurses": 2}),
    ("Respiratory distress", "Critical", 4, {"Beds": 1, "ICU beds": 1, "Doctors": 1, "Nurses": 2, "Ventilators": 1}),
    ("Major trauma", "Urgent", 3, {"Beds": 1, "Doctors": 1, "Nurses": 2, "Operating rooms": 1}),
    ("Appendicitis", "Urgent", 3, {"Beds": 1, "Doctors": 1, "Nurses": 1, "Operating rooms": 1}),
    ("Fracture", "Routine", 2, {"Beds": 1, "Doctors": 1, "Nurses": 1}),
    ("Infection", "Routine", 2, {"Beds": 1, "Doctors": 1, "Nurses": 1}),
]

DISCHARGE_PRIORITY = {"Routine": 0, "Urgent": 1, "Critical": 2}
DISCHARGE_MINUTES = {"Routine": 90, "Urgent": 150, "Critical": 240}


def seed_patients():
    names = ["A. Patel", "B. Williams", "C. Garcia", "D. Chen", "E. Smith", "F. Okafor", "G. Jones", "H. Khan"]
    now = datetime.now().replace(second=0, microsecond=0)
    patients = []
    for index, name in enumerate(names):
        condition, acuity, priority, requirements = CONDITIONS[index % len(CONDITIONS)]
        patients.append({
            "id": f"P-{index + 1:03d}", "name": name, "condition": condition,
            "acuity": acuity, "priority": priority,
            "arrival": now - timedelta(minutes=(index + 1) * 12),
            "requirements": requirements.copy(), "status": "Waiting",
            "admitted_at": None, "discharged_at": None, "waiting_minutes": 0,
            "reason": "Not yet assessed",
        })
    return patients


def initialize():
    st.session_state.clock = datetime.now().replace(second=0, microsecond=0)
    st.session_state.patients = seed_patients()
    st.session_state.resources = {name: {"total": total, "failed": 0} for name, total in RESOURCE_DEFAULTS.items()}
    st.session_state.mode = "Normal"
    st.session_state.event_log = ["Simulation initialized"]
    st.session_state.ticks = 0


def ensure_state():
    if "patients" not in st.session_state:
        initialize()
    for patient in st.session_state.patients:
        patient.setdefault("discharged_at", None)


def effective_capacity(name):
    resource = st.session_state.resources[name]
    return max(0, resource["total"] - resource["failed"])


def current_usage(name):
    return sum(patient["requirements"].get(name, 0) for patient in st.session_state.patients if patient["status"] == "Admitted")


def available_capacity(name):
    return max(0, effective_capacity(name) - current_usage(name))


def can_admit(patient):
    blocked = []
    for resource, needed in patient["requirements"].items():
        if available_capacity(resource) < needed:
            blocked.append(f"{resource} ({available_capacity(resource)}/{needed})")
    return blocked


def refresh_waiting_times():
    for patient in st.session_state.patients:
        if patient["status"] == "Waiting":
            patient["waiting_minutes"] = max(0, int((st.session_state.clock - patient["arrival"]).total_seconds() // 60))


def patient_stay_minutes(patient):
    if patient["status"] != "Admitted" or patient["admitted_at"] is None:
        return 0
    return max(0, int((st.session_state.clock - patient["admitted_at"]).total_seconds() // 60))


def discharge_priority(patient):
    return (DISCHARGE_PRIORITY.get(patient["acuity"], 99), -patient_stay_minutes(patient), patient["admitted_at"] or st.session_state.clock)


def recommend_discharge_candidates():
    return sorted(
        [patient for patient in st.session_state.patients if patient["status"] == "Admitted"],
        key=discharge_priority,
    )


def allocate_patients():
    refresh_waiting_times()
    admitted_count = 0
    waiting = sorted(
        [p for p in st.session_state.patients if p["status"] == "Waiting"],
        key=lambda p: (p["priority"], p["arrival"]),
    )
    for patient in waiting:
        blocked = can_admit(patient)
        if not blocked:
            patient["status"] = "Admitted"
            patient["admitted_at"] = st.session_state.clock
            patient["reason"] = "Allocated successfully"
            admitted_count += 1
        else:
            patient["reason"] = "Waiting for " + ", ".join(blocked)
    return admitted_count


def discharge_patient(patient_id):
    """Discharge one admitted patient; their requirements stop counting as used."""
    for patient in st.session_state.patients:
        if patient["id"] == patient_id and patient["status"] == "Admitted":
            patient["status"] = "Discharged"
            patient["discharged_at"] = st.session_state.clock
            st.session_state.event_log.insert(0, f"{patient['id']} ({patient['name']}) was discharged; resources released.")
            return True
    return False


def discharge_selected(patient_ids):
    count = sum(discharge_patient(patient_id) for patient_id in patient_ids)
    if count:
        allocate_patients()
    return count


def auto_discharge_patients():
    """Discharge stable patients after required stay length and with the longest admissions first."""
    discharged_count = 0
    for patient in recommend_discharge_candidates():
        min_minutes = DISCHARGE_MINUTES.get(patient["acuity"], 9999)
        if patient_stay_minutes(patient) >= min_minutes:
            if discharge_patient(patient["id"]):
                discharged_count += 1
    if discharged_count:
        allocate_patients()
    return discharged_count


def add_patient():
    index = len(st.session_state.patients) + 1
    condition, acuity, priority, requirements = random.choice(CONDITIONS)
    st.session_state.patients.append({
        "id": f"P-{index:03d}", "name": f"Simulated patient {index}",
        "condition": condition, "acuity": acuity, "priority": priority,
        "arrival": st.session_state.clock, "requirements": requirements.copy(),
        "status": "Waiting", "admitted_at": None, "discharged_at": None,
        "waiting_minutes": 0, "reason": "Not yet assessed",
    })


def simulate_failure():
    candidates = [name for name, resource in st.session_state.resources.items() if resource["total"] - resource["failed"] > 0]
    if not candidates:
        return "No operational resources are available to fail."
    name = random.choice(candidates)
    st.session_state.resources[name]["failed"] += 1
    message = f"Unexpected failure: one {name} became unavailable."
    st.session_state.event_log.insert(0, message)
    return message


def advance_simulation():
    st.session_state.clock += timedelta(minutes=15)
    st.session_state.ticks += 1
    if st.session_state.mode in {"Emergency surge", "Disaster"} and random.random() < 0.7:
        add_patient()
        st.session_state.event_log.insert(0, "A new emergency arrival entered the waiting list.")
    if random.random() < 0.18:
        simulate_failure()
    auto_discharge_patients()
    admitted_count = allocate_patients()
    st.session_state.event_log.insert(0, f"Advanced to {st.session_state.clock:%H:%M}; admitted {admitted_count} patient(s).")


ensure_state()

st.sidebar.title("⚙️ Simulation controls")
mode = st.sidebar.selectbox("Operating mode", list(MODE_LABELS), index=list(MODE_LABELS).index(st.session_state.mode), format_func=lambda value: f"{value} — {MODE_LABELS[value]}")
if mode != st.session_state.mode:
    st.session_state.mode = mode
    if mode == "Staff shortage":
        st.session_state.resources["Doctors"]["total"] = max(1, RESOURCE_DEFAULTS["Doctors"] // 2)
        st.session_state.resources["Nurses"]["total"] = max(1, RESOURCE_DEFAULTS["Nurses"] // 2)
    elif mode == "Resource shortage":
        for name in ["Beds", "ICU beds", "Ventilators"]:
            st.session_state.resources[name]["total"] = max(1, RESOURCE_DEFAULTS[name] // 2)
    elif mode == "Disaster":
        st.session_state.resources["Doctors"]["total"] = max(1, RESOURCE_DEFAULTS["Doctors"] // 2)
        st.session_state.resources["Nurses"]["total"] = max(1, RESOURCE_DEFAULTS["Nurses"] // 2)
        st.session_state.resources["Beds"]["total"] = max(1, RESOURCE_DEFAULTS["Beds"] // 2)

st.sidebar.caption(MODE_LABELS[st.session_state.mode])
if st.sidebar.button("▶️ Advance 15 minutes", use_container_width=True):
    advance_simulation()
if st.sidebar.button("👤 Add simulated patient", use_container_width=True):
    add_patient()
    allocate_patients()
if st.sidebar.button("⚡ Simulate unexpected failure", use_container_width=True):
    st.sidebar.warning(simulate_failure())
    allocate_patients()
if st.sidebar.button("🔄 Reset simulation", use_container_width=True):
    initialize()
    st.rerun()

st.title("🏥 MedFlow")
st.caption("Hospital management and resource-allocation simulation")
st.info(f"Simulation time: **{st.session_state.clock:%Y-%m-%d %H:%M}**  ·  Mode: **{st.session_state.mode}**  ·  Tick: **{st.session_state.ticks}**")

st.subheader("Hospital resources")
st.caption("Change total capacity below. Used capacity is calculated from admitted patients; failed units are unavailable.")
resource_columns = st.columns(3)
for index, name in enumerate(st.session_state.resources):
    resource = st.session_state.resources[name]
    with resource_columns[index % 3]:
        resource["total"] = st.number_input(f"{name} — total", min_value=0, max_value=200, value=int(resource["total"]), key=f"total_{name}")
        used = current_usage(name)
        st.metric("Available", available_capacity(name), delta=f"{used} used")
        if resource["failed"]:
            st.caption(f"⚠️ Failed units: {resource['failed']}")

waiting = [p for p in st.session_state.patients if p["status"] == "Waiting"]
admitted = [p for p in st.session_state.patients if p["status"] == "Admitted"]
discharged = [p for p in st.session_state.patients if p["status"] == "Discharged"]
refresh_waiting_times()
avg_wait = sum(p["waiting_minutes"] for p in waiting) / len(waiting) if waiting else 0
m1, m2, m3, m4 = st.columns(4)
m1.metric("Waiting", len(waiting))
m2.metric("Currently admitted", len(admitted))
m3.metric("Average waiting time", f"{avg_wait:.0f} min")
m4.metric("Discharged", len(discharged))

st.subheader("Patient flow")
tab_waiting, tab_admitted, tab_discharged, tab_resources, tab_events = st.tabs(["🕒 Waiting list", "🛏️ Currently admitted", "✅ Discharged", "📊 Resource utilization", "📋 Event log"])

with tab_waiting:
    if waiting:
        rows = []
        for position, patient in enumerate(sorted(waiting, key=lambda p: (p["priority"], p["arrival"])), 1):
            rows.append({"#": position, "ID": patient["id"], "Patient": patient["name"], "Condition": patient["condition"], "Acuity": patient["acuity"], "Waiting": f"{patient['waiting_minutes']} min", "Why waiting": patient["reason"]})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.success("No patients are waiting.")

with tab_admitted:
    if admitted:
        sorted_admitted = sorted(admitted, key=discharge_priority)
        st.caption("Discharge order is based on condition severity and length of stay: the least critical patients with the longest admission time are recommended first.")
        selected = st.multiselect(
            "Patients to discharge",
            options=[p["id"] for p in sorted_admitted],
            format_func=lambda patient_id: next(
                f"{p['name']} ({p['id']}) — {p['acuity']} — {patient_stay_minutes(p)} min admitted"
                for p in sorted_admitted
                if p["id"] == patient_id
            ),
            key="discharge_selection",
        )
        if st.button("✅ Discharge selected patient(s)", type="primary", disabled=not selected):
            count = discharge_selected(selected)
            st.success(f"Discharged {count} patient(s); their resources are now available.")
            st.rerun()
        rows = [{
            "Priority": DISCHARGE_PRIORITY.get(p["acuity"], 99),
            "ID": p["id"],
            "Patient": p["name"],
            "Condition": p["condition"],
            "Acuity": p["acuity"],
            "Admitted": p["admitted_at"].strftime("%H:%M"),
            "Stay": f"{patient_stay_minutes(p)} min",
            "Resources": ", ".join(f"{k}: {v}" for k, v in p["requirements"].items()),
        } for p in sorted_admitted]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.warning("No patients are currently admitted.")

with tab_discharged:
    if discharged:
        rows = [{"ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Admitted": p["admitted_at"].strftime("%H:%M"), "Discharged": p["discharged_at"].strftime("%H:%M")} for p in discharged]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No patients have been discharged yet.")

with tab_resources:
    rows = []
    for name in st.session_state.resources:
        total = st.session_state.resources[name]["total"]
        failed = st.session_state.resources[name]["failed"]
        used = current_usage(name)
        rows.append({"Resource": name, "Total": total, "Used": used, "Failed": failed, "Available": available_capacity(name), "Utilization": f"{(used / total * 100) if total else 0:.0f}%"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

with tab_events:
    for event in st.session_state.event_log[:12]:
        st.write(f"• {event}")

shortages = []
for name, resource in st.session_state.resources.items():
    if resource["failed"] > 0:
        shortages.append(f"{resource['failed']} {name} failed")
if any(p["acuity"] == "Critical" and p["status"] == "Waiting" for p in waiting):
    shortages.append("critical patient waiting")
if shortages:
    st.divider()
    st.warning("⚠️ Silent alarm — " + "; ".join(shortages) + ". Review staffing or resource allocation.", icon="⚠️")
