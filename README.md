# MedFlow

MedFlow is a Streamlit hospital-management simulation for experimenting with patient prioritization and resource allocation.

## Run locally

```bash
git clone https://github.com/tanaypunj/medflow
cd medflow
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Features

✨ MedFlow Features
1. Hospital Resource Simulation: Beds, ICU beds, doctors, nurses, operating rooms, ventilators with adjustable totals and reductions.

2. Patient Flow Management: Simulates arrivals, admissions, and discharges with conditions (cardiac, trauma, infection, etc.) and acuity levels (Critical, Urgent, Routine).

3. Operating Modes: Normal, Emergency surge, Staff shortage, Resource shortage, Disaster, and fully customizable scenarios.

4. Arrival Patterns: Walk-in vs ambulance arrivals, configurable intervals, ambulance share, and pattern types.

5. Priority-Based Admission: Patients admitted using a formula combining urgency, waiting time, and resource criticality.

6. Simulation Controls: Sidebar options for speed, pause/resume, skip time, reset, trigger failures, and adjust ambulance fleet/resources.

7. Dynamic Events: Automatic patient arrivals, resource failures/repairs, ICU transfers, and auto-discharge after defined durations.

8. Metrics & Monitoring: Tracks waiting times, throughput, ambulance arrivals, and resource utilization with historical logging.

9. Interactive Dashboard: Tabs for waiting list, admitted patients, discharged patients, resource usage, statistics, strategy comparison, and event log.

10. Real-Time Updates: Auto-refresh every second for continuous simulation progress


## Credits: Tanay Punj, Supratim Basu, Tanmay Anand, Github-CoPilot
