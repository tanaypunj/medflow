# MedFlow

MedFlow is a Streamlit hospital-management simulation for experimenting with patient prioritization and resource allocation.

## Run locally

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
streamlit run app.py
```

## Features

- Priority-ordered waiting list and currently admitted patients
- Editable hospital capacity for beds, ICU beds, staff, operating rooms, and ventilators
- Available, used, failed, and utilization views for every resource
- Waiting-time calculation based on simulation time
- Normal, emergency surge, staff shortage, resource shortage, disaster, and custom modes
- Automatic and manual unexpected resource failures
- Silent alarm at the bottom of the dashboard when a critical patient is waiting or resources fail

This is an educational simulation and is not intended for real-world clinical decision-making.
