# Saarthi: a voice co-driver and AI office for small Indian fleets

> Built for Gnani AI's **Great Indian AI Internship Challenge** (solo entry).
> **Gnani models used:** **Prisma v3.0** (speech-to-text) for every voice note, and **Timbre v2.5** (text-to-speech) for every spoken reply and alert.

75% of Indian truck operators own fewer than 5 trucks. They run on phone calls, paper diaries and guesswork, and can't afford GPS hardware or fleet software. Saarthi gives every truck a **Driver Agent** the driver simply *talks* to in Hindi or Hinglish, and gives the office a **CEO Agent** that oversees all the trucks and keeps the owner updated.

## What it does

**Driver Agent (one per truck), over Telegram voice notes**
- "Bhaiya 4500 ka diesel daala, 48 litre" → the agent reads it back ("diesel ₹4,500, 48 litre — sahi hai?") → saved only after the driver says *haan*.
- "Tyre phat gaya" → the owner is alerted with the location → the nearest mechanics and tyre shops are sent to the driver → if nothing is reachable, **Fleet Mesh** asks nearby trucks (from any opted-in fleet) for help.
- Accident or injury → the driver gets **112 / 108** to tap immediately, and the owner is alerted. The AI never talks to emergency services itself.
- Fatigue check-ins while driving (yes/no only); two missed check-ins alert the owner.
- Weather and route ahead, document expiry, trip expenses so far.
- Answers stay short while the truck is moving; longer conversations happen only when parked.

**CEO Agent (one per fleet), for the owner**
- "Sab gaadiyan kahan hain?", "aaj ka kharcha?", "kaun se documents expire ho rahe hain?"
- **Freight quotes from a deterministic cost engine:** diesel + tolls + driver pay + maintenance + empty return leg → break-even, recommended quote, and profit on the client's offer. The LLM never does the money maths.
- Escalation is decided by **rules in code**: emergencies are handed to the owner immediately, breakdowns trigger an alert, and routine entries go into the daily digest.
- Client **tracking link** using the driver's phone location, with **no GPS hardware**.
- Optional **n8n** workflows: an 8 PM report, document expiry alerts, and expenses synced to Google Sheets.

```
Driver voice ─► Prisma ─► Driver Agent ──events──► CEO Agent ──► Owner (Timbre voice + text)
      ▲                      │    ▲                   │
      └──── Timbre ◄─────────┘    └── Fleet Mesh ◄────┘ (nearby trucks of other fleets)
```

## Run it

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # add your keys (never commit .env)
python -m saarthi.seed --demo                       # synthetic demo fleets
python -m saarthi.main
```
1. In Telegram, the owner sends `/start OWNER-MEWAR` to the bot, and each driver sends `/start MW-T1` (codes are printed by the seeder).
2. Drivers keep **Share Live Location** on and send voice notes.
3. For client tracking links reachable from outside, expose port 8080 with a free tunnel (`cloudflared tunnel --url http://localhost:8080`) and set `PUBLIC_BASE_URL`.

To try it without Telegram, run `python -m saarthi.sim`; it gives you a text chat as the driver or owner.
To run the tests, use `pytest` (they run offline with a scripted LLM).

## Accuracy evaluation (Prisma in real noise)
`eval/run_eval.py` measures word error rate and **amount accuracy** for each noise condition (fuel pump, moving cab, highway roadside, phone speaker), **with and without** the domain keyterms list (`data/keyterms.txt`, which includes Rajasthani place names). Recordings are made with the speakers' consent, stay private, and are never committed; only the aggregated results are published.

Results will be filled in after the pilot.

## Honest limitations
- The LLM reasoning step uses an OpenAI-compatible endpoint (Gemini or Groq by default). Gnani's Evon is open-weights but not hosted on the challenge API; you can point `LLM_BASE_URL` at a self-hosted Evon.
- Without a Google Maps key, routes come from OSRM and have **no live traffic**. The agent says so rather than guessing.
- Nearby-mechanic data comes from OpenStreetMap and is incomplete in rural areas. Fleet Mesh is the fallback.
- Telegram bots can't place phone calls. Owner alerts are voice notes plus a one-tap button to open the driver's chat or call.
- All fleets, plates and people in this repo and in the demo are **synthetic**.

## Project layout
`saarthi/agents/driver.py` Driver Agent · `agents/ceo.py` CEO Agent · `agents/mesh.py` Fleet Mesh · `costing.py` cost engine · `speech.py` Gnani Prisma/Timbre · `safety.py` escalation rules · `fatigue.py` check-ins + live location · `web.py` tracking page + n8n API · `telegram_app.py` bots.
