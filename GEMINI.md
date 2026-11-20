# Saarthi AI — project rules for the Antigravity agent

## Product
Saarthi AI is an agentic co-pilot for Indian truck fleets (owner, driver, shipper, consignee).
Ten agents: Saarthi Core, Dispatch, Route Oracle, Recovery, Freight Matcher, Compliance, Customer Desk,
Detention, Saarthi Dash (driver robot), Fleet Analyst (BigQuery).

## Hard rules
- Never write API keys, tokens or passwords in code or docs. Secrets live only in `.env` (gitignored) locally
  and in Secret Manager on Google Cloud.
- All demo data is synthetic. Label anything simulated (calls, bookings, messages) as "simulated".
- Driver-facing text: short, warm Hinglish, safety first, address the driver as "<name> ji".
- Owner-facing text: concrete rupee numbers, max 3 sentences.
- Do not use any film character's name, look or voice for Saarthi Dash. It is an original design.
- Keep pricing out of product copy (pricing is undecided).

## Tech
- Backend: Python 3.14, aiohttp (`app/`), started with `python run.py` on port 8080.
- AI: Gemini via `google-genai` (`app/gemini.py`). Use `USE_VERTEX=1` on Cloud Run (no key).
- Data: BigQuery dataset `saarthi_fleet`, table `trips` (schema in `app/analyst.py`), region `us-central1`.
- Google Cloud project: `saarthi-ai-os`. Cloud Run region: `asia-south1`.
- Every Gemini call must have a timeout and a fallback so the demo never stalls.
- Run `python tests/e2e_test.py` after changes. All checks must pass.
