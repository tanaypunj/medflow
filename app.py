import random
import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="MedFlow", page_icon="🏥", layout="wide")

STEP_SECONDS = 30
MAX_EVENTS = 200
MAX_HISTORY = 300
MAX_DISCHARGED_KEPT = 500
DEFAULT_SPEED = 30.0  # the original code effectively ran at ~30x; keep that pace

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
    "Resource shortage": "Reduced beds and equipment capacity",
    "Disaster": "Mass-casualty event",
    "Custom": "Custom arrival rate",
}
# Minutes between arrivals (Custom is set in the sidebar).
ARRIVAL_INTERVALS = {
    "Normal": 30,
    "Emergency surge": 5,
    "Staff shortage": 15,
    "Resource shortage": 12,
    "Disaster": 2,
    "Custom": 20,
}
# Modes that actually change capacity (previously they only changed arrival rate).
MODE_CAPACITY_FACTORS = {
    "Staff shortage": {"Doctors": 0.6, "Nurses": 0.6},
    "Resource shortage": {"Beds": 0.7, "ICU beds": 0.5, "Ventilators": 0.5, "Operating rooms": 0.67},
}
# Weights follow the order of CONDITIONS below.
CONDITION_WEIGHTS = {
    "Emergency surge": [3, 3, 3, 1, 1, 1],
    "Disaster": [2, 1, 8, 1, 3, 1],
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


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
def log(message):
    events = st.session_state.event_log
    events.insert(0, f"[{st.session_state.clock:%H:%M}] {message}")
    del events[MAX_EVENTS:]


def patient_record(pid, name, condition, acuity, urgency, requirements, arrival, wait_seconds=0.0):
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
        "queue_wait_seconds": float(wait_seconds),
        "reason": "Not yet assessed",
    }


def seed_patients(now):
    names = ["A. Patel", "B. Williams", "C. Garcia", "D. Chen", "E. Smith", "F. Okafor", "G. Jones", "H. Khan"]
    patients = []
    for i, name in enumerate(names):
        condition, acuity, urgency, requirements = CONDITIONS[i % len(CONDITIONS)]
        minutes_ago = (i + 1) * 12
        patients.append(
            patient_record(
                f"P-{i + 1:03d}", name, condition, acuity, urgency, requirements,
                now - timedelta(minutes=minutes_ago),
                wait_seconds=minutes_ago * 60,  # arrival was in the past, so wait must reflect it
            )
        )
    return patients


def initialize():
    ss = st.session_state
    now = datetime.now().replace(microsecond=0)
    ss.clock = now
    ss.sim_start = now
    ss.patients = seed_patients(now)
    ss.next_patient_number = len(ss.patients) + 1
    ss.treated_total = 0
    ss.resources = {name: {"total": total, "repairs": []} for name, total in RESOURCE_DEFAULTS.items()}
    ss.mode = "Normal"
    ss.speed = DEFAULT_SPEED
    ss.paused = False
    ss.event_log = ["Simulation initialized"]
    ss.ticks = 0
    ss.arrival_accumulator = 0.0
    ss.sim_remainder = 0.0
    ss.last_wall_time = time.monotonic()
    ss.metrics_history = []
    # Stale widget state would otherwise overwrite the freshly reset totals.
    for name in RESOURCE_DEFAULTS:
        ss.pop(f"total_{name}", None)
    ss.pop("custom_interval", None)


def ensure_state():
    if "patients" not in st.session_state:
        initialize()
    allocate_patients()


# ----------------------------------------------------------------------------
# Capacity
# ----------------------------------------------------------------------------
def failed_units(name):
    return len(st.session_state.resources[name]["repairs"])


def mode_capacity(name):
    factor = MODE_CAPACITY_FACTORS.get(st.session_state.mode, {}).get(name, 1.0)
    return int(st.session_state.resources[name]["total"] * factor)


def effective_capacity(name):
    return max(0, mode_capacity(name) - failed_units(name))


def usage_snapshot():
    """One pass over patients instead of one pass per (patient x resource) lookup."""
    usage = dict.fromkeys(st.session_state.resources, 0)
    for patient in st.session_state.patients:
        if patient["status"] == "Admitted":
            for name, amount in patient["requirements"].items():
                usage[name] = usage.get(name, 0) + amount
    return usage


def available_capacity(name, usage):
    return max(0, effective_capacity(name) - usage.get(name, 0))


def utilization(name, usage):
    capacity = effective_capacity(name)
    if capacity == 0:
        return 100.0 if usage.get(name, 0) else 0.0
    return usage.get(name, 0) / capacity * 100


# ----------------------------------------------------------------------------
# Patient logic
# ----------------------------------------------------------------------------
def stay_minutes(patient):
    if patient["admitted_at"] is None:
        return 0
    end = patient["discharged_at"] or st.session_state.clock
    return max(0, int((end - patient["admitted_at"]).total_seconds() // 60))


def resource_criticality(patient):
    return sum(CRITICALITY_WEIGHTS.get(name, 1) for name, amount in patient["requirements"].items() if amount > 0)


def patient_priority(patient):
    """P = U×10 + W×1 + R×2, where W is elapsed queue wait in minutes."""
    if patient["status"] != "Waiting":
        return -1.0
    return patient["urgency"] * 10 + patient["queue_wait_seconds"] / 60 + resource_criticality(patient) * 2


def allocate_patients():
    """Admit by priority. A blocked patient reserves the resources it is short of,
    so lower-priority patients cannot keep snatching them (starvation)."""
    ss = st.session_state
    waiting = sorted((p for p in ss.patients if p["status"] == "Waiting"), key=patient_priority, reverse=True)
    if not waiting:
        return 0
    usage = usage_snapshot()
    reserved = set()
    admitted = 0
    for patient in waiting:
        short = []
        for name, needed in patient["requirements"].items():
            free = effective_capacity(name) - usage.get(name, 0)
            if free < needed:
                short.append((name, needed, max(0, free)))
        if short:
            patient["reason"] = "Waiting for " + ", ".join(f"{n} ({f}/{q})" for n, q, f in short)
            # Only reserve what could ever be satisfied, or one impossible request blocks everyone.
            reserved.update(n for n, q, _ in short if q <= mode_capacity(n))
            continue
        held = [n for n in patient["requirements"] if n in reserved]
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
    clock = st.session_state.clock
    for name, resource in st.session_state.resources.items():
        due = [t for t in resource["repairs"] if t <= clock]
        if due:
            resource["repairs"] = [t for t in resource["repairs"] if t > clock]
            log(f"{name} repair completed; {len(due)} unit(s) restored.")


def transfer_critical_patients():
    usage = None
    for patient in st.session_state.patients:
        if (
            patient["status"] != "Admitted"
            or patient["acuity"] != "Critical"
            or patient["location"] != "ICU"
            or stay_minutes(patient) < CRITICAL_TRANSFER_MINUTES
        ):
            continue
        reqs = patient["requirements"]
        # The patient already holds a bed, so no free bed is needed to step down.
        # (The old check deadlocked: full beds -> no transfer -> ICU patients never leave.)
        if "Beds" not in reqs:
            usage = usage or usage_snapshot()
            if available_capacity("Beds", usage) < 1:
                continue
            reqs["Beds"] = 1
        reqs.pop("ICU beds", None)
        reqs.pop("Ventilators", None)
        patient["location"] = "Normal bed"
        log(f"{patient['id']} transferred from ICU to a normal bed.")


def auto_discharge():
    count = 0
    for patient in st.session_state.patients:
        if patient["status"] != "Admitted":
            continue
        if patient["acuity"] == "Critical" and patient["location"] != "Normal bed":
            continue
        if stay_minutes(patient) >= DISCHARGE_MINUTES.get(patient["acuity"], 9999):
            release_patient(patient)
            count += 1
    return count


def arrival_interval():
    ss = st.session_state
    if ss.mode == "Custom":
        return float(ss.get("custom_interval", 20.0))
    return float(ARRIVAL_INTERVALS[ss.mode])


def add_patient():
    ss = st.session_state
    number = ss.next_patient_number  # counter, not len(): discharged patients get pruned
    ss.next_patient_number += 1
    condition, acuity, urgency, requirements = random.choices(
        CONDITIONS, weights=CONDITION_WEIGHTS.get(ss.mode)
    )[0]
    patient = patient_record(
        f"P-{number:03d}", f"Simulated patient {number}", condition, acuity, urgency, requirements, ss.clock
    )
    ss.patients.append(patient)
    log(f"New {acuity.lower()} patient arrived: {patient['id']} ({condition}).")


def simulate_failure():
    ss = st.session_state
    candidates = [
        name for name, r in ss.resources.items()
        if name != "Beds" and r["total"] - len(r["repairs"]) > 0
    ]
    if not candidates:
        return
    name = random.choice(candidates)
    minutes = random.randint(60, 180) if name == "ICU beds" else random.randint(30, 120)
    ss.resources[name]["repairs"].append(ss.clock + timedelta(minutes=minutes))
    log(f"Unexpected failure: one {name} unit is down; repair due in {minutes} simulated minute(s).")


def prune_discharged():
    ss = st.session_state
    discharged = [p for p in ss.patients if p["status"] == "Discharged"]
    excess = len(discharged) - MAX_DISCHARGED_KEPT
    if excess > 0:
        drop = {p["id"] for p in sorted(discharged, key=lambda p: p["discharged_at"])[:excess]}
        ss.patients = [p for p in ss.patients if p["id"] not in drop]


def record_metrics():
    ss = st.session_state
    waits = [p["queue_wait_seconds"] / 60 for p in ss.patients if p["status"] == "Waiting"]
    critical_waiting = sum(1 for p in ss.patients if p["status"] == "Waiting" and p["acuity"] == "Critical")
    admitted = sum(1 for p in ss.patients if p["status"] == "Admitted")
    usage = usage_snapshot()
    hours = max((ss.clock - ss.sim_start).total_seconds() / 3600, 1 / 60)
    entry = {
        "time": ss.clock,
        "Average waiting time": sum(waits) / len(waits) if waits else 0,
        "Maximum waiting time": max(waits, default=0),
        "Critical patients waiting": critical_waiting,
        "Patients treated": ss.treated_total,
        "Throughput (patients/hr)": ss.treated_total / hours,
        "Admitted": admitted,
    }
    for name in ss.resources:
        entry[f"Utilization: {name}"] = utilization(name, usage)
    ss.metrics_history.append(entry)
    del ss.metrics_history[:-MAX_HISTORY]


def advance_simulation(simulated_seconds):
    """Advance in 30 s steps, carrying the remainder so speed is honored exactly."""
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
        interval = max(arrival_interval(), 0.1)
        while ss.arrival_accumulator >= interval:
            ss.arrival_accumulator -= interval
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
    if ss.get("paused") or elapsed <= 0:
        return
    advance_simulation(elapsed * ss.speed)


# ----------------------------------------------------------------------------
# Callbacks (safe place to modify widget-bound state)
# ----------------------------------------------------------------------------
def skip_minutes(minutes):
    advance_simulation(minutes * 60)
    st.session_state.last_wall_time = time.monotonic()


def reset_simulation():
    initialize()


def reset_arrival_accumulator():
    st.session_state.arrival_accumulator = 0.0


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
ensure_state()
st_autorefresh(interval=1000, key="medflow_clock")
update_from_real_clock()

st.sidebar.title("⚙️ Simulation controls")
st.sidebar.selectbox(
    "Operating mode",
    list(MODE_LABELS),
    key="mode",
    format_func=lambda x: f"{x} — {MODE_LABELS[x]}",
    on_change=reset_arrival_accumulator,
)
if st.session_state.mode == "Custom":
    st.sidebar.number_input(
        "Minutes between arrivals", min_value=0.5, max_value=240.0, value=20.0, step=0.5, key="custom_interval"
    )
st.sidebar.number_input(
    "Simulation speed",
    min_value=0.1,
    max_value=500.0,
    step=0.1,
    format="%.1f",
    key="speed",
    help="Simulated seconds per real second. Maximum 500×.",
)
st.sidebar.checkbox("⏸ Pause simulation", key="paused")
st.sidebar.caption("Queue wait grows while a patient is waiting; admission occurs as soon as the required resources are free.")

skip_columns = st.sidebar.columns(5)
for column, minutes in zip(skip_columns, (1, 5, 10, 15, 20)):
    column.button(f"+{minutes}m", key=f"skip_{minutes}", on_click=skip_minutes, args=(minutes,), use_container_width=True)

st.sidebar.button("⚡ Simulate unexpected failure", on_click=simulate_failure, use_container_width=True)
st.sidebar.button("🔄 Reset simulation", on_click=reset_simulation, use_container_width=True)

st.title("🏥 MedFlow")
st.caption("Hospital management and resource-allocation simulation (educational model, not for clinical use)")
paused_note = " · ⏸ **paused**" if st.session_state.paused else ""
st.info(
    f"Simulation time: **{st.session_state.clock:%Y-%m-%d %H:%M:%S}** · Mode: **{st.session_state.mode}** "
    f"· Speed: **{st.session_state.speed:.1f}×**{paused_note}"
)

usage = usage_snapshot()

st.subheader("Hospital resources")
st.caption("Priority formula: P = U×10 + W×1 + R×2, where W is elapsed queue wait in minutes.")
resource_columns = st.columns(3)
for i, (name, resource) in enumerate(st.session_state.resources.items()):
    with resource_columns[i % 3]:
        resource["total"] = st.number_input(
            f"{name} — total", min_value=0, max_value=200, value=int(resource["total"]), key=f"total_{name}"
        )
        st.metric("Available", available_capacity(name, usage), delta=f"{usage.get(name, 0)} used", delta_color="off")
        if mode_capacity(name) != resource["total"]:
            st.caption(f"Mode-adjusted capacity: {mode_capacity(name)}")
        if resource["repairs"]:
            st.caption(f"⚠️ Failed units: {len(resource['repairs'])} · next repair {min(resource['repairs']):%H:%M}")

patients = st.session_state.patients
waiting = [p for p in patients if p["status"] == "Waiting"]
admitted = [p for p in patients if p["status"] == "Admitted"]
discharged = [p for p in patients if p["status"] == "Discharged"]
avg_wait = sum(p["queue_wait_seconds"] for p in waiting) / len(waiting) / 60 if waiting else 0.0
max_wait = max((p["queue_wait_seconds"] for p in waiting), default=0.0) / 60
treated_total = st.session_state.treated_total

m1, m2, m3, m4 = st.columns(4)
m1.metric("Waiting", len(waiting))
m2.metric("Currently admitted", len(admitted))
m3.metric("Average waiting time", f"{avg_wait:.1f} min")
m4.metric("Patients treated", treated_total)

st.subheader("Patient flow")
tab_wait, tab_admit, tab_dis, tab_res, tab_stats, tab_log = st.tabs([
    "🕒 Waiting list", "🛏️ Currently admitted", "✅ Discharged",
    "📊 Resource utilization", "📈 Statistics", "📋 Event log",
])

with tab_wait:
    rows = [{
        "#": i,
        "ID": p["id"],
        "Patient": p["name"],
        "Condition": p["condition"],
        "Acuity": p["acuity"],
        "Priority P": f"{patient_priority(p):.1f}",
        "Queue wait": f"{p['queue_wait_seconds'] / 60:.1f} min",
        "Reason": p["reason"],
    } for i, p in enumerate(sorted(waiting, key=patient_priority, reverse=True)[:200], start=1)]
    if rows:
        if len(waiting) > 200:
            st.caption(f"Showing the top 200 of {len(waiting)} waiting patients.")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.success("No patients are waiting.")

with tab_admit:
    if admitted:
        ordered = sorted(admitted, key=stay_minutes, reverse=True)
        labels = {p["id"]: f"{p['name']} ({p['id']}) — {p['location']} — {stay_minutes(p)} min" for p in ordered}
        selected = st.multiselect("Patients to discharge", list(labels), format_func=labels.get)
        if st.button("✅ Discharge selected patient(s)", type="primary", disabled=not selected):
            for pid in selected:
                discharge_patient(pid)
            allocate_patients()
            st.rerun()

        st.dataframe(pd.DataFrame([{
            "ID": p["id"],
            "Patient": p["name"],
            "Condition": p["condition"],
            "Acuity": p["acuity"],
            "Location": p["location"],
            "Stay": f"{stay_minutes(p)} min",
            "Resources": ", ".join(f"{n}:{q}" for n, q in p["requirements"].items()),
        } for p in ordered]), use_container_width=True, hide_index=True)
    else:
        st.warning("No patients are currently admitted.")

with tab_dis:
    recent = sorted((p for p in discharged if p["discharged_at"]), key=lambda p: p["discharged_at"], reverse=True)[:200]
    if recent:
        st.caption(f"Most recent {len(recent)} of {treated_total} discharged patients.")
        st.dataframe(pd.DataFrame([{
            "ID": p["id"],
            "Patient": p["name"],
            "Condition": p["condition"],
            "Acuity": p["acuity"],
            "Queue wait": f"{p['queue_wait_seconds'] / 60:.1f} min",
            "Discharged": p["discharged_at"].strftime("%H:%M:%S"),
        } for p in recent]), use_container_width=True, hide_index=True)
    else:
        st.info("No patients have been discharged yet.")

with tab_res:
    st.dataframe(pd.DataFrame([{
        "Resource": name,
        "Total": r["total"],
        "Mode-adjusted": mode_capacity(name),
        "Failed": len(r["repairs"]),
        "Used": usage.get(name, 0),
        "Available": available_capacity(name, usage),
        "Utilization": f"{utilization(name, usage):.0f}%",
    } for name, r in st.session_state.resources.items()]), use_container_width=True, hide_index=True)

with tab_stats:
    history = pd.DataFrame(st.session_state.metrics_history)
    critical_waiting = sum(p["acuity"] == "Critical" for p in waiting)
    hours = max((st.session_state.clock - st.session_state.sim_start).total_seconds() / 3600, 1 / 60)
    overall_util = sum(min(utilization(n, usage), 100.0) for n in st.session_state.resources) / len(st.session_state.resources)

    s1, s2, s3 = st.columns(3)
    s1.metric("Average waiting time", f"{avg_wait:.1f} min")
    s2.metric("Maximum waiting time", f"{max_wait:.1f} min")
    s3.metric("Critical patients waiting", critical_waiting)

    s4, s5, s6 = st.columns(3)
    s4.metric("Patients treated", treated_total)
    s5.metric("Throughput", f"{treated_total / hours:.1f} patients/hr")
    s6.metric("Resource utilization", f"{overall_util:.1f}%")

    if not history.empty:
        st.caption("Waiting time (minutes)")
        st.line_chart(history.set_index("time")[["Average waiting time", "Maximum waiting time"]], use_container_width=True)
        util_cols = [c for c in history.columns if c.startswith("Utilization:")]
        st.caption("Resource utilization (%) over simulation time")
        st.line_chart(history.set_index("time")[util_cols], use_container_width=True)
        st.caption("Patients treated (cumulative)")
        st.line_chart(history.set_index("time")[["Patients treated"]], use_container_width=True)
        st.caption("Throughput (patients per simulated hour)")
        st.line_chart(history.set_index("time")[["Throughput (patients/hr)"]], use_container_width=True)
    else:
        st.info("Statistics will appear after the simulation advances.")

with tab_log:
    for event in st.session_state.event_log[:12]:
        st.write(f"• {event}")

shortages = [f"{len(r['repairs'])} {name} failed" for name, r in st.session_state.resources.items() if r["repairs"]]
if any(p["acuity"] == "Critical" for p in waiting):
    shortages.append("critical patient waiting")
if shortages:
    st.divider()
    st.warning("⚠️ Silent alarm — " + "; ".join(shortages) + ". Review staffing or resource allocation.", icon="⚠️")
