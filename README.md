# Saarthi AI — agentic co-pilot for India's truck fleets

Google Startup Buildathon India 2026 · Mumbai · built on Google Gemini (Google Cloud deploy pending credits).

**Demo data is simulated.** Calls, bookings and third-party messages are simulated and labelled. Weather (Open-Meteo) and road routes (OSRM) are live.

## Run locally

```powershell
cd "D:\saarthi buildathon"
.\.venv\Scripts\python run.py
```

Then open:
- http://localhost:8080 — **Fleet Command Center** (owner)
- http://localhost:8080/dash — **Saarthi Dash** robot (driver phone on the dashboard)

Put your Gemini key in `.env` as `GEMINI_API_KEY=...`. Without a key the app runs in offline mode: all playbooks still work, and the replies come from templates.

## What it does

| Chaos | Agents that resolve it |
|---|---|
| New order | Dispatch Agent picks the truck; Route Oracle predicts route, live weather, traffic, ETA and cost |
| Sudden breakdown | Recovery Agent: diagnosis (Gemini), nearest mechanic, relief truck, customer ETA |
| Empty truck | Freight Matcher ranks the load board and books a return load |
| Delivery refused | Customer Desk calls consignee and shipper, compares wait / return / storage |
| Cancelled after dispatch | Exposure calculation, cancellation fee, alternative freight |
| Waiting 8 h at warehouse | Detention Agent: clock, dock call, escalation, invoice |
| Documents expiring | Compliance Agent renews and verifies |
| Tired driver | Saarthi Dash rest stop in Hindi |
| "Which truck is losing money?" | Fleet Analyst: Gemini text-to-SQL over the BigQuery schema |

Voice: Hindi, Hinglish and English via the mic button. The owner chat uses Gemini function calling to start the agents.

## Structure

- `app/fleet.py` — synthetic fleet world
- `app/engine.py` — event bus and the 8 agent playbooks
- `app/predict.py` — Route Oracle (OSRM + Open-Meteo + traffic model)
- `app/gemini.py` — Gemini brain (API key locally, Vertex AI on Cloud Run)
- `app/analyst.py` — Fleet Analyst (BigQuery, or a local mirror while billing is pending)
- `static/` — command center and the Saarthi Dash UI
- `tests/e2e_test.py` — end-to-end check of every feature
- `tools/make_demo_video.py` — records the narrated 3-minute demo
