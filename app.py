import random
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="MedFlow", page_icon="🏥", layout="wide")

RESOURCE_DEFAULTS = {"Beds": 30, "ICU beds": 6, "Doctors": 8, "Nurses": 16, "Operating rooms": 3, "Ventilators": 5}
MODE_LABELS = {"Normal": "Routine operations", "Emergency surge": "Higher emergency arrivals and acuity", "Staff shortage": "Reduced clinical staffing", "Resource shortage": "Reduced beds and equipment", "Disaster": "Severe surge with multiple constraints", "Custom": "Use configured resources"}
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
CRITICAL_TRANSFER_MINUTES = 120


def patient_record(patient_id, name, condition, acuity, priority, requirements, arrival):
    return {"id": patient_id, "name": name, "condition": condition, "acuity": acuity, "priority": priority,
            "arrival": arrival, "requirements": requirements.copy(), "base_requirements": requirements.copy(),
            "status": "Waiting", "location": "Waiting list", "admitted_at": None, "discharged_at": None,
            "waiting_minutes": 0, "reason": "Not yet assessed"}


def seed_patients():
    now = datetime.now().replace(second=0, microsecond=0)
    names = ["A. Patel", "B. Williams", "C. Garcia", "D. Chen", "E. Smith", "F. Okafor", "G. Jones", "H. Khan"]
    return [patient_record(f"P-{i + 1:03d}", name, *CONDITIONS[i % len(CONDITIONS)], now - timedelta(minutes=(i + 1) * 12)) for i, name in enumerate(names)]


def initialize():
    st.session_state.clock = datetime.now().replace(second=0, microsecond=0)
    st.session_state.patients = seed_patients()
    st.session_state.resources = {name: {"total": total, "failed": 0, "repair_at": None} for name, total in RESOURCE_DEFAULTS.items()}
    st.session_state.mode = "Normal"
    st.session_state.event_log = ["Simulation initialized"]
    st.session_state.ticks = 0


def ensure_state():
    if "patients" not in st.session_state:
        initialize()
    for p in st.session_state.patients:
        p.setdefault("discharged_at", None)
        p.setdefault("location", "ICU" if p.get("status") == "Admitted" and p.get("acuity") == "Critical" else "Normal bed")
        p.setdefault("base_requirements", p.get("requirements", {}).copy())
    for resource in st.session_state.resources.values():
        resource.setdefault("repair_at", None)


def effective_capacity(name):
    resource = st.session_state.resources[name]
    return max(0, resource["total"] - resource["failed"])


def current_usage(name):
    return sum(p["requirements"].get(name, 0) for p in st.session_state.patients if p["status"] == "Admitted")


def available_capacity(name):
    return max(0, effective_capacity(name) - current_usage(name))


def refresh_waiting_times():
    for p in st.session_state.patients:
        if p["status"] == "Waiting":
            p["waiting_minutes"] = max(0, int((st.session_state.clock - p["arrival"]).total_seconds() // 60))


def stay_minutes(p):
    if p["admitted_at"] is None:
        return 0
    return max(0, int(((p["discharged_at"] or st.session_state.clock) - p["admitted_at"]).total_seconds() // 60))


def can_admit(p):
    return [f"{name} ({available_capacity(name)}/{needed})" for name, needed in p["requirements"].items() if available_capacity(name) < needed]


def allocate_patients():
    refresh_waiting_times()
    count = 0
    waiting = sorted((p for p in st.session_state.patients if p["status"] == "Waiting"), key=lambda p: (p["priority"], p["arrival"]))
    for p in waiting:
        blocked = can_admit(p)
        if blocked:
            p["reason"] = "Waiting for " + ", ".join(blocked)
            continue
        p["status"] = "Admitted"
        p["location"] = "ICU" if p["acuity"] == "Critical" and "ICU beds" in p["requirements"] else "Normal bed"
        p["admitted_at"] = st.session_state.clock
        p["reason"] = "Allocated successfully"
        count += 1
    return count


def discharge_patient(patient_id):
    for p in st.session_state.patients:
        if p["id"] == patient_id and p["status"] == "Admitted":
            old_location = p["location"]
            p["status"] = "Discharged"
            p["discharged_at"] = st.session_state.clock
            p["location"] = "Discharged"
            st.session_state.event_log.insert(0, f"{p['id']} ({p['name']}) discharged from {old_location}; resources released.")
            return True
    return False


def discharge_selected(ids):
    count = sum(discharge_patient(pid) for pid in ids)
    if count:
        allocate_patients()
    return count


def repair_resources():
    for name, resource in st.session_state.resources.items():
        if resource["repair_at"] and st.session_state.clock >= resource["repair_at"]:
            repaired = resource["failed"]
            resource["failed"] = 0
            resource["repair_at"] = None
            st.session_state.event_log.insert(0, f"{name} repair completed; {repaired} unit(s) restored.")


def transfer_critical_patients():
    for p in st.session_state.patients:
        if p["status"] != "Admitted" or p["acuity"] != "Critical" or p["location"] != "ICU":
            continue
        if stay_minutes(p) < CRITICAL_TRANSFER_MINUTES or available_capacity("Beds") < 1:
            continue
        p["requirements"] = p["base_requirements"].copy()
        p["requirements"].pop("ICU beds", None)
        p["requirements"].pop("Ventilators", None)
        p["requirements"]["Beds"] = 1
        p["location"] = "Normal bed"
        st.session_state.event_log.insert(0, f"{p['id']} transferred from ICU to a normal bed after {stay_minutes(p)} minutes.")


def auto_discharge():
    ordered = sorted((p for p in st.session_state.patients if p["status"] == "Admitted"), key=lambda p: (DISCHARGE_PRIORITY.get(p["acuity"], 99), -stay_minutes(p)))
    count = 0
    for p in ordered:
        if p["acuity"] == "Critical" and p["location"] != "Normal bed":
            continue
        if stay_minutes(p) >= DISCHARGE_MINUTES.get(p["acuity"], 9999) and discharge_patient(p["id"]):
            count += 1
    if count:
        allocate_patients()
    return count


def add_patient():
    i = len(st.session_state.patients) + 1
    condition, acuity, priority, requirements = random.choice(CONDITIONS)
    st.session_state.patients.append(patient_record(f"P-{i:03d}", f"Simulated patient {i}", condition, acuity, priority, requirements, st.session_state.clock))


def simulate_failure():
    candidates = [name for name, resource in st.session_state.resources.items() if name != "Beds" and resource["total"] - resource["failed"] > 0]
    if not candidates:
        return "No eligible operational resource is available to fail."
    name = random.choice(candidates)
    resource = st.session_state.resources[name]
    resource["failed"] += 1
    if name == "ICU beds":
        hours = random.randint(1, 3)
        resource["repair_at"] = st.session_state.clock + timedelta(hours=hours)
        message = f"Unexpected ICU failure; repair due in {hours} hour(s)."
    else:
        message = f"Unexpected failure: one {name} became unavailable."
    st.session_state.event_log.insert(0, message)
    return message


def evaluate_patients(minutes):
    """Advance the simulation in 30-second evaluation steps, then render one update."""
    steps = max(1, int(minutes * 60 / 30))
    for _ in range(steps):
        st.session_state.clock += timedelta(seconds=30)
        repair_resources()
        transfer_critical_patients()
        auto_discharge()
        allocate_patients()
    st.session_state.ticks += steps
    st.session_state.event_log.insert(0, f"Evaluated patients for {minutes} minute(s) in {steps} thirty-second step(s).")


ensure_state()
st.sidebar.title("⚙️ Simulation controls")
mode = st.sidebar.selectbox("Operating mode", list(MODE_LABELS), index=list(MODE_LABELS).index(st.session_state.mode), format_func=lambda x: f"{x} — {MODE_LABELS[x]}")
if mode != st.session_state.mode:
    st.session_state.mode = mode
    if mode == "Staff shortage":
        st.session_state.resources["Doctors"]["total"] = max(1, RESOURCE_DEFAULTS["Doctors"] // 2)
        st.session_state.resources["Nurses"]["total"] = max(1, RESOURCE_DEFAULTS["Nurses"] // 2)
    elif mode == "Resource shortage":
        for name in ("ICU beds", "Ventilators"):
            st.session_state.resources[name]["total"] = max(1, RESOURCE_DEFAULTS[name] // 2)
    elif mode == "Disaster":
        for name in ("Doctors", "Nurses", "ICU beds"):
            st.session_state.resources[name]["total"] = max(1, RESOURCE_DEFAULTS[name] // 2)
st.sidebar.caption(MODE_LABELS[st.session_state.mode])
st.sidebar.caption("Patient status is evaluated every 30 simulated seconds.")

skip_columns = st.sidebar.columns(5)
for column, minutes in zip(skip_columns, (1, 5, 10, 15, 20)):
    if column.button(f"+{minutes}m", key=f"skip_{minutes}", use_container_width=True):
        evaluate_patients(minutes)
        st.rerun()
if st.sidebar.button("👤 Add simulated patient", use_container_width=True):
    add_patient(); allocate_patients(); st.rerun()
if st.sidebar.button("⚡ Simulate unexpected failure", use_container_width=True):
    simulate_failure(); allocate_patients(); st.rerun()
if st.sidebar.button("🔄 Reset simulation", use_container_width=True):
    initialize(); st.rerun()

st.title("🏥 MedFlow")
st.caption("Hospital management and resource-allocation simulation")
st.info(f"Simulation time: **{st.session_state.clock:%Y-%m-%d %H:%M:%S}** · Mode: **{st.session_state.mode}** · Evaluations: **{st.session_state.ticks}**")

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
refresh_waiting_times()
m1, m2, m3, m4 = st.columns(4)
m1.metric("Waiting", len(waiting))
m2.metric("Currently admitted", len(admitted))
m3.metric("Average waiting time", f"{(sum(p['waiting_minutes'] for p in waiting) / len(waiting) if waiting else 0):.0f} min")
m4.metric("Discharged", len(discharged))

st.subheader("Patient flow")
tab_wait, tab_admit, tab_dis, tab_res, tab_log = st.tabs(["🕒 Waiting list", "🛏️ Currently admitted", "✅ Discharged", "📊 Resource utilization", "📋 Event log"])
with tab_wait:
    rows = [{"#": i, "ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Acuity": p["acuity"], "Waiting": f"{p['waiting_minutes']} min", "Why waiting": p["reason"]} for i, p in enumerate(sorted(waiting, key=lambda p: (p["priority"], p["arrival"])), 1)]
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.success("No patients are waiting.")
with tab_admit:
    if admitted:
        ordered = sorted(admitted, key=lambda p: (DISCHARGE_PRIORITY.get(p["acuity"], 99), -stay_minutes(p)))
        st.caption("Critical patients transfer from ICU to a normal bed after 120 minutes when available, then discharge after their condition-specific stay.")
        selected = st.multiselect("Patients to discharge", [p["id"] for p in ordered], format_func=lambda pid: next(f"{p['name']} ({pid}) — {p['location']} — {stay_minutes(p)} min" for p in ordered if p["id"] == pid), key="discharge_selection")
        if st.button("✅ Discharge selected patient(s)", type="primary", disabled=not selected):
            discharge_selected(selected)
            st.rerun()
        rows = [{"ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Acuity": p["acuity"], "Location": p["location"], "Stay": f"{stay_minutes(p)} min", "Resources": ", ".join(f"{k}: {v}" for k, v in p["requirements"].items())} for p in ordered]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.warning("No patients are currently admitted.")
with tab_dis:
    rows = [{"ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Discharged": p["discharged_at"].strftime("%H:%M:%S")} for p in discharged]
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No patients have been discharged yet.")
with tab_res:
    rows = [{"Resource": name, "Total": resource["total"], "Used": current_usage(name), "Failed": resource["failed"], "Available": available_capacity(name), "Utilization": f"{(current_usage(name) / resource['total'] * 100) if resource['total'] else 0:.0f}%"} for name, resource in st.session_state.resources.items()]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
with tab_log:
    for event in st.session_state.event_log[:12]:
        st.write(f"• {event}")

shortages = [f"{resource['failed']} {name} failed" for name, resource in st.session_state.resources.items() if resource["failed"]]
if any(p["acuity"] == "Critical" and p["status"] == "Waiting" for p in waiting):
    shortages.append("critical patient waiting")
if shortages:
    st.divider()
    st.warning("⚠️ Silent alarm — " + "; ".join(shortages) + ". Review staffing or resource allocation.", icon="⚠️")
