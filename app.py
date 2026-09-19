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


def seed_patients():
    now = datetime.now().replace(second=0, microsecond=0)
    names = ["A. Patel", "B. Williams", "C. Garcia", "D. Chen", "E. Smith", "F. Okafor", "G. Jones", "H. Khan"]
    patients = []
    for i, name in enumerate(names):
        condition, acuity, priority, requirements = CONDITIONS[i % len(CONDITIONS)]
        patients.append({"id": f"P-{i + 1:03d}", "name": name, "condition": condition, "acuity": acuity, "priority": priority, "arrival": now - timedelta(minutes=(i + 1) * 12), "requirements": requirements.copy(), "base_requirements": requirements.copy(), "status": "Waiting", "location": "Waiting list", "admitted_at": None, "discharged_at": None, "waiting_minutes": 0, "reason": "Not yet assessed"})
    return patients


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
    for name, resource in st.session_state.resources.items():
        resource.setdefault("repair_at", None)


def effective_capacity(name):
    return max(0, st.session_state.resources[name]["total"] - st.session_state.resources[name]["failed"])


def current_usage(name):
    return sum(p["requirements"].get(name, 0) for p in st.session_state.patients if p["status"] == "Admitted")


def available_capacity(name):
    return max(0, effective_capacity(name) - current_usage(name))


def can_admit(patient):
    return [f"{r} ({available_capacity(r)}/{n})" for r, n in patient["requirements"].items() if available_capacity(r) < n]


def refresh_waiting_times():
    for p in st.session_state.patients:
        if p["status"] == "Waiting":
            p["waiting_minutes"] = max(0, int((st.session_state.clock - p["arrival"]).total_seconds() // 60))


def stay_minutes(p):
    if p["admitted_at"] is None:
        return 0
    end = p["discharged_at"] or st.session_state.clock
    return max(0, int((end - p["admitted_at"]).total_seconds() // 60))


def discharge_key(p):
    return (DISCHARGE_PRIORITY.get(p["acuity"], 99), -stay_minutes(p), p["admitted_at"] or st.session_state.clock)


def allocate_patients():
    refresh_waiting_times()
    count = 0
    for p in sorted([x for x in st.session_state.patients if x["status"] == "Waiting"], key=lambda x: (x["priority"], x["arrival"])):
        blocked = can_admit(p)
        if not blocked:
            p["status"] = "Admitted"
            p["location"] = "ICU" if p["acuity"] == "Critical" and "ICU beds" in p["requirements"] else "Normal bed"
            p["admitted_at"] = st.session_state.clock
            p["reason"] = "Allocated successfully"
            count += 1
        else:
            p["reason"] = "Waiting for " + ", ".join(blocked)
    return count


def discharge_patient(patient_id):
    for p in st.session_state.patients:
        if p["id"] == patient_id and p["status"] == "Admitted":
            p["status"] = "Discharged"
            p["discharged_at"] = st.session_state.clock
            p["location"] = "Discharged"
            st.session_state.event_log.insert(0, f"{p['id']} ({p['name']}) discharged from {p.get('location', 'hospital')}; resources released.")
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
    count = 0
    for p in list(st.session_state.patients):
        if p["status"] != "Admitted" or p["acuity"] != "Critical" or p["location"] != "ICU":
            continue
        if stay_minutes(p) < CRITICAL_TRANSFER_MINUTES:
            continue
        # ICU care is released; the patient continues in a normal bed.
        if available_capacity("Beds") < 1:
            continue
        p["requirements"] = p["base_requirements"].copy()
        p["requirements"].pop("ICU beds", None)
        p["requirements"].pop("Ventilators", None)
        p["requirements"]["Beds"] = 1
        p["location"] = "Normal bed"
        count += 1
        st.session_state.event_log.insert(0, f"{p['id']} transferred from ICU to a normal bed after {stay_minutes(p)} minutes.")
    return count


def auto_discharge():
    count = 0
    for p in sorted([x for x in st.session_state.patients if x["status"] == "Admitted"], key=discharge_key):
        required = DISCHARGE_MINUTES.get(p["acuity"], 9999)
        # Critical patients are discharged only after their ICU-to-bed transfer.
        if p["acuity"] == "Critical" and p["location"] != "Normal bed":
            continue
        if stay_minutes(p) >= required and discharge_patient(p["id"]):
            count += 1
    if count:
        allocate_patients()
    return count


def add_patient():
    i = len(st.session_state.patients) + 1
    condition, acuity, priority, requirements = random.choice(CONDITIONS)
    st.session_state.patients.append({"id": f"P-{i:03d}", "name": f"Simulated patient {i}", "condition": condition, "acuity": acuity, "priority": priority, "arrival": st.session_state.clock, "requirements": requirements.copy(), "base_requirements": requirements.copy(), "status": "Waiting", "location": "Waiting list", "admitted_at": None, "discharged_at": None, "waiting_minutes": 0, "reason": "Not yet assessed"})


def simulate_failure():
    # Normal beds are intentionally excluded: only ICU beds and other equipment/staff can fail.
    candidates = [n for n, r in st.session_state.resources.items() if n != "Beds" and r["total"] - r["failed"] > 0]
    if not candidates:
        return "No eligible operational resource is available to fail."
    name = random.choice(candidates)
    resource = st.session_state.resources[name]
    resource["failed"] += 1
    if name == "ICU beds":
        repair_hours = random.randint(1, 3)
        resource["repair_at"] = st.session_state.clock + timedelta(hours=repair_hours)
        message = f"Unexpected ICU failure: {resource['failed']} ICU bed(s) unavailable; repair due in {repair_hours} hour(s)."
    else:
        message = f"Unexpected failure: one {name} became unavailable."
    st.session_state.event_log.insert(0, message)
    return message


def advance():
    st.session_state.clock += timedelta(minutes=15)
    st.session_state.ticks += 1
    repair_resources()
    if st.session_state.mode in {"Emergency surge", "Disaster"} and random.random() < 0.7:
        add_patient()
        st.session_state.event_log.insert(0, "A new emergency arrival entered the waiting list.")
    if random.random() < 0.18:
        simulate_failure()
    transfer_critical_patients()
    auto_discharge()
    admitted = allocate_patients()
    st.session_state.event_log.insert(0, f"Advanced to {st.session_state.clock:%H:%M}; admitted {admitted} patient(s).")


ensure_state()
st.sidebar.title("⚙️ Simulation controls")
mode = st.sidebar.selectbox("Operating mode", list(MODE_LABELS), index=list(MODE_LABELS).index(st.session_state.mode), format_func=lambda x: f"{x} — {MODE_LABELS[x]}")
if mode != st.session_state.mode:
    st.session_state.mode = mode
    if mode == "Staff shortage":
        st.session_state.resources["Doctors"]["total"] = max(1, RESOURCE_DEFAULTS["Doctors"] // 2)
        st.session_state.resources["Nurses"]["total"] = max(1, RESOURCE_DEFAULTS["Nurses"] // 2)
    elif mode == "Resource shortage":
        for n in ["ICU beds", "Ventilators"]:
            st.session_state.resources[n]["total"] = max(1, RESOURCE_DEFAULTS[n] // 2)
    elif mode == "Disaster":
        for n in ["Doctors", "Nurses", "ICU beds"]:
            st.session_state.resources[n]["total"] = max(1, RESOURCE_DEFAULTS[n] // 2)
st.sidebar.caption(MODE_LABELS[st.session_state.mode])
if st.sidebar.button("▶️ Advance 15 minutes", use_container_width=True):
    advance()
if st.sidebar.button("👤 Add simulated patient", use_container_width=True):
    add_patient(); allocate_patients()
if st.sidebar.button("⚡ Simulate unexpected failure", use_container_width=True):
    st.sidebar.warning(simulate_failure()); allocate_patients()
if st.sidebar.button("🔄 Reset simulation", use_container_width=True):
    initialize(); st.rerun()

st.title("🏥 MedFlow")
st.caption("Hospital management and resource-allocation simulation")
st.info(f"Simulation time: **{st.session_state.clock:%Y-%m-%d %H:%M}** · Mode: **{st.session_state.mode}** · Tick: **{st.session_state.ticks}**")

st.subheader("Hospital resources")
st.caption("Normal beds never fail. ICU failures are automatically repaired within 1–3 simulated hours.")
cols = st.columns(3)
for i, name in enumerate(st.session_state.resources):
    resource = st.session_state.resources[name]
    with cols[i % 3]:
        resource["total"] = st.number_input(f"{name} — total", min_value=0, max_value=200, value=int(resource["total"]), key=f"total_{name}")
        st.metric("Available", available_capacity(name), delta=f"{current_usage(name)} used")
        if resource["failed"]:
            repair = f" · repair due {resource['repair_at']:%H:%M}" if resource["repair_at"] else ""
            st.caption(f"⚠️ Failed units: {resource['failed']}{repair}")

waiting = [p for p in st.session_state.patients if p["status"] == "Waiting"]
admitted = [p for p in st.session_state.patients if p["status"] == "Admitted"]
discharged = [p for p in st.session_state.patients if p["status"] == "Discharged"]
refresh_waiting_times()
m1, m2, m3, m4 = st.columns(4)
m1.metric("Waiting", len(waiting)); m2.metric("Currently admitted", len(admitted)); m3.metric("Average waiting time", f"{(sum(p['waiting_minutes'] for p in waiting) / len(waiting) if waiting else 0):.0f} min"); m4.metric("Discharged", len(discharged))

st.subheader("Patient flow")
tab_wait, tab_admit, tab_dis, tab_res, tab_log = st.tabs(["🕒 Waiting list", "🛏️ Currently admitted", "✅ Discharged", "📊 Resource utilization", "📋 Event log"])
with tab_wait:
    rows = [{"#": i, "ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Acuity": p["acuity"], "Waiting": f"{p['waiting_minutes']} min", "Why waiting": p["reason"]} for i, p in enumerate(sorted(waiting, key=lambda x: (x["priority"], x["arrival"])), 1)]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True) if rows else st.success("No patients are waiting.")
with tab_admit:
    if admitted:
        ordered = sorted(admitted, key=discharge_key)
        st.caption("Critical patients stabilize in ICU, transfer to a normal bed after 120 minutes when available, then become eligible for discharge after 240 total minutes.")
        selected = st.multiselect("Patients to discharge", [p["id"] for p in ordered], format_func=lambda pid: next(f"{p['name']} ({pid}) — {p['location']} — {stay_minutes(p)} min" for p in ordered if p["id"] == pid), key="discharge_selection")
        if st.button("✅ Discharge selected patient(s)", type="primary", disabled=not selected):
            discharge_selected(selected); st.rerun()
        st.dataframe(pd.DataFrame([{ "ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Acuity": p["acuity"], "Location": p["location"], "Stay": f"{stay_minutes(p)} min", "Resources": ", ".join(f"{k}: {v}" for k, v in p["requirements"].items())} for p in ordered]), use_container_width=True, hide_index=True)
    else:
        st.warning("No patients are currently admitted.")
with tab_dis:
    st.dataframe(pd.DataFrame([{ "ID": p["id"], "Patient": p["name"], "Condition": p["condition"], "Discharged": p["discharged_at"].strftime("%H:%M") } for p in discharged]), use_container_width=True, hide_index=True) if discharged else st.info("No patients have been discharged yet.")
with tab_res:
    st.dataframe(pd.DataFrame([{ "Resource": n, "Total": r["total"], "Used": current_usage(n), "Failed": r["failed"], "Available": available_capacity(n), "Utilization": f"{(current_usage(n) / r['total'] * 100) if r['total'] else 0:.0f}%" } for n, r in st.session_state.resources.items()]), use_container_width=True, hide_index=True)
with tab_log:
    for event in st.session_state.event_log[:12]: st.write(f"• {event}")

shortages = [f"{r['failed']} {n} failed" for n, r in st.session_state.resources.items() if r["failed"]]
if any(p["acuity"] == "Critical" and p["status"] == "Waiting" for p in waiting): shortages.append("critical patient waiting")
if shortages:
    st.divider(); st.warning("⚠️ Silent alarm — " + "; ".join(shortages) + ". Review staffing or resource allocation.", icon="⚠️")
