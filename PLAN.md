# Saarthi — agentic voice operations for small fleets

**What it is:** every truck gets a **Driver Agent** the driver talks to in Hindi, Hinglish or a regional language. The office gets a **CEO Agent** that oversees all the trucks, keeps the owner updated, and hands anything serious straight to the owner.
**Gnani models:** Prisma v3.0 (speech-to-text) for every voice note, and Timbre v2.5 (text-to-speech) for every spoken reply and alert.

```
 Driver phone (Telegram)            Office / owner phone (Telegram)
   voice note / location               voice or text
          |                                   |
   [Prisma STT] ─────┐               ┌───── [Prisma STT]
                     v               v
             DRIVER AGENT  ──event──> CEO AGENT ──escalate──> OWNER (voice alert + call button)
             (one per truck)  <─task──  (one per fleet)
                 |   ^                     |
                 |   └── MESH: nearby Driver Agents of other opted-in fleets (peer help)
                 v                         v
   tools: expenses · freight · breakdown · tools: fleet status · P&L · freight quote ·
   mechanics nearby · weather · route ·          documents expiry · tracking link ·
   fatigue check · emergency 112 · location      daily digest (n8n -> Sheets)
                 |
           [Timbre TTS] -> voice reply to driver
```

## Your plan mapped to the build

| Your feature | How it works | Data source (free) |
|---|---|---|
| Handle driver calls | Driver sends Telegram voice notes; the agent replies by voice. A live call mode is a stretch goal. | Prisma + Timbre |
| Freight + cost logging | The LLM extracts the amount and the agent **reads it back for confirmation** before saving | SQLite |
| Sudden breakdown | Classify the problem → alert the CEO Agent → find the nearest mechanic or tyre shop → tap-to-call → update the owner | OpenStreetMap Overpass |
| Help from another fleet's agent | **Fleet Mesh**: find opted-in trucks within 25 km, ask that driver by voice, relay messages without sharing phone numbers | own DB |
| Keep the driver awake | Fatigue check-ins every 2 h and at night (yes/no only); two missed check-ins alert the owner | scheduler |
| Better route + traffic | Route + alternatives; traffic only if a Google key is set (otherwise stated honestly as "no traffic data") | OSRM / Google Routes (optional) |
| Weather analysis | Weather sampled along the route; warnings for rain, fog, heat | Open-Meteo |
| Location | Telegram live location, with a client tracking link on a map page — **no GPS hardware** | Telegram + Leaflet/OSM |
| Documentation expiry | Track insurance, permit, fitness, PUC and licence dates; the CEO Agent warns 30, 7 and 1 day before expiry | SQLite + n8n cron |
| CEO Agent keeps the owner updated | Routine updates go into a daily digest; important events trigger an immediate voice + text alert | Timbre |
| Hand over to the owner | **Rule-based**, not LLM-guessed: accident or injury → driver gets a 112 button + owner gets an alert immediately | code |
| Deal costing | **Deterministic engine**: diesel + tolls + driver pay + maintenance + empty return leg → break-even, quote, profit | code (LLM only explains the numbers) |

## Safety and honesty rules (built into the code)
- Numbers are always confirmed by the driver before saving, and money maths is never done by the LLM.
- While the truck is moving the agent gives short prompts and asks yes/no questions only; longer conversations happen when parked.
- For emergencies the driver is connected to 112 directly. The AI never talks to emergency services.
- Only synthetic fleets, numbers and plates appear in the demo and the repo (see RULES.md).

## Build order (cut from the bottom if time runs short)
1. **Core:** voice → Prisma → Driver Agent → confirmation → Timbre reply; expenses and freight. *Must have.*
2. **Breakdown:** mechanics nearby + owner escalation + 112. *Must have.*
3. **CEO Agent:** owner Q&A, freight quote / P&L, daily digest. *Must have.*
4. **Accuracy evaluation:** WER and amount accuracy per noise condition, with and without keyterms. *Must have.*
5. Location + client tracking link.
6. Fleet Mesh peer help.
7. Documents expiry + n8n digest to Sheets.
8. Weather / route.
9. Fatigue check-ins.

## Schedule
- Nov 3–4: steps 1–4 code complete; first driver test; Post 1.
- Nov 5–6: steps 5–9; pilot with 3–5 drivers.
- Nov 7: Post 2 (real driver clip); fix failures.
- Nov 8: accuracy runs on consented recordings; pilot numbers.
- Nov 9: video + Post 3; repo made public.
- Nov 10: submit early.
