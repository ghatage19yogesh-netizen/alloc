# Allocation Planner (Streamlit demo)

A planner-facing app for monthly warehouse allocation: set limits, press **Run allocation**,
review the plan and its warnings, then approve and export.

Runs on **synthetic sample data** with a simplified engine (`engine.py`) and the open-source
HiGHS solver (via SciPy) — no Gurobi licence and no company data needed.

## Screens
1. **Set up & run** — month, warehouse min/max share and space, editable products and demand,
   pre-run checks (the run is blocked until red checks pass).
2. **See the plan** — cost, units, overflow, space used per warehouse × storage type, cost chart, CSV export, approve.
3. **Check warnings** — overflow, missing cost lanes, rule-based placements, medium-velocity limits.
4. **Who it's for** — the user persona.

## How the engine works
1. Sort products: high/medium → optimizer, low → slow seller, no demand → even split.
2. Slow sellers go to the single cheapest warehouse with space (deterministic, no random pick).
3. Space used by rule-based stock is subtracted.
4. Linear program: minimise shipping cost + overflow penalty, subject to
   - every zone's demand met exactly
   - each warehouse's share between Min % and Max % (rule-based stock included)
   - storage space per warehouse × type (soft — overflow pays the penalty)
   - medium-velocity products limited to their 2 cheapest warehouses
5. Missing cost lanes are filled with the warehouse average (not the minimum).

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Share it on Streamlit Community Cloud (free)
1. Create a new GitHub repository and upload `app.py`, `engine.py`, `requirements.txt`, `README.md`.
2. Go to https://share.streamlit.io and sign in with GitHub.
3. Click **Create app** → **Deploy a public app from GitHub** → pick the repo, branch `main`, main file `app.py` → **Deploy**.
4. After 1–2 minutes you get a link like `https://<your-app>.streamlit.app` — share it.
   To limit who can open it, use **Settings → Sharing** in the app's menu.
