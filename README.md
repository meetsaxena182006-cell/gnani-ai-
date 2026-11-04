# Saarthi: a voice co-driver and AI office for small Indian fleets

> Built for Gnani AI's **Great Indian AI Internship Challenge** (solo entry).
> **Gnani models used:** **Prisma v3.0** (speech-to-text) for every voice note, and **Timbre v2.5** (text-to-speech) for every spoken reply and alert.

75% of Indian truck operators own fewer than 5 trucks. They run on phone calls, paper diaries and guesswork, and can't afford GPS hardware or fleet software. Saarthi gives every truck a **live voice companion**: the phone sits in a holder on the dashboard, and the driver just talks, never touching it. The office gets a **CEO Agent** that oversees all the trucks and keeps the owner updated.

## What it does

**Driver Agent: a companion that lives on the driver's phone (always on, hands-free)**
- Chats like a friend and **checks in every 1–2 hours** ("Ram Ram boss, sab theek? tabiyat badhiya?"). Two unanswered check-ins alert the owner.
- "Saarthi, 4500 ka diesel daala, 48 litre" → "diesel ₹4,500, 48 litre — sahi hai?" → saved only after *haan*.
- "Tyre phat gaya!" → the owner is alerted with the location → the nearest mechanic is found → "unko call lagaun?" → *haan* → the phone dials.
- **Fleet Mesh:** nearby Saarthi companions on the same road ask their drivers "aage ek truck mushkil mein hai, madad kar sakte hain?" The first *haan* is matched, everyone else is told help was found, and everyone hears when the problem is solved.
- Accident or injury → the owner is alerted immediately → "112 lagaun?". The AI never talks to emergency services itself.
- Answers in the driver's language: Hindi, Punjabi, Gujarati, Marathi, Bengali, Tamil, Telugu, Kannada, Malayalam and English. Prisma detects the language and Timbre switches to that language's voice. Marwari/Mewari is understood as Hindi with Rajasthani keyterms.
- Stays quiet while the driver talks to family or the helper. It responds only when addressed ("Saarthi …"), during an open conversation, or when it hears emergency or breakdown words.
- Answers stay short and yes/no while the truck is moving.

**CEO watchdog: nothing gets missed**
- Every GPS fix is checked against **542 NHAI toll plazas** (government data). When a truck crosses tolls and the driver hasn't logged them, the CEO Agent gets the companion to ask: "Driver sahab, Paduna aur Khandi Obari toll cross kiya, kitna laga? FASTag ya cash?"
- **FASTag trucks:** tolls are logged automatically when the GPS shows a plaza crossing, so the driver is never asked. The estimate is replaced with the **actual deduction** when the FASTag statement (CSV upload) or debit SMS (forwarded to `/api/fastag/sms`) arrives. A deduction with **no GPS crossing** is flagged to the owner as a possible wrong or double charge to dispute.
- **Fuel pumps:** when the truck stops at a fuel station (OpenStreetMap) and drives off without logging diesel, the companion asks "Demo Fuels pump pe 12 minute ruke the, diesel bharwaya? kitne litre, kitne ka?".
- Everything (spoken entries, automatic FASTag tolls, freight received) flows into Google Sheets every hour via n8n.
- Also catches unexplained long stops (asks the driver if all is well), heading away from the destination, overspeeding (gentle warning), trucks gone offline (owner alerted), and diesel that doesn't add up (km driven vs litres logged).

**Companion brain**
- Gets fresh **situation awareness** every turn: time, speed, hours driving, nearest landmark, unreported tolls, last diesel fill.
- A fast model handles conversation, and `think_deeply` sends hard questions ("raat tak pahunchenge?", "kahan rukein?") to a stronger reasoning model along with the truck's real data.
- **Road analysis:** route options, truck-adjusted ETA, NHAI tolls on the way with expected cost, weather expected at each stretch *when the truck gets there*, and night-driving hours.
- Talks with the driver when he is bored, by voice only, (cricket, family, dhaba food, old songs, shayari) and plays music: radio streams you configure, or a YouTube Music search. Music volume drops automatically while Saarthi speaks.

**CEO Agent: always live on the server, reachable from the owner's laptop (`/office`) and Telegram voice notes**
- Live map (street or satellite) of every truck, companion status, open issues, and a feed of agent activity.
- "Sab gaadiyan kahan hain?", "aaj ka kharcha?", "kaun se documents expire ho rahe hain?"
- **Freight quotes from a deterministic cost engine:** diesel + tolls (**NHAI plazas on the actual route**) + driver pay + maintenance + empty return leg → break-even, recommended quote, and profit on the client's offer.
- `analyze_business` sends strategic questions to the reasoning model with the full fleet picture.
- Escalation is decided by **rules in code**: emergencies are handed to the owner immediately, breakdowns trigger an alert, and routine entries go into the daily digest.
- Client **tracking link** from the driver's phone GPS, with **no GPS hardware**. Optional **n8n** workflows: daily report, document expiry, expenses synced to Google Sheets.

```
Phone in dashboard holder ⇄ WebRTC ⇄ [VAD → Prisma v3.0 → Guard → LLM+tools → Timbre v2.5] ⇄ CEO Agent ⇄ Owner
          GPS over data channel ─┘                     └─ Fleet Mesh ⇄ companions of nearby trucks
```

## Run it

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # add your keys (never commit .env)
python -m saarthi.seed --demo                       # synthetic demo fleets; prints each truck's companion link
python -m saarthi.main
```
1. **Phones need HTTPS** for the microphone, so expose the server with a free tunnel: `cloudflared tunnel --url http://localhost:8080`. Set `PUBLIC_BASE_URL` to the tunnel URL and re-run the seeder to get the links.
2. **Driver:** open the printed `/driver?v=…&k=…` link on the phone, add it to the home screen, tap **Start** once, and put the phone in the holder. The screen stays awake (dimmed).
3. **Owner:** open `/office` on the laptop (key = `API_SHARED_SECRET`). For Telegram, send `/start OWNER-MEWAR` to the bot.
4. On mobile data, WebRTC may need a TURN server. Set `ICE_SERVERS` in `.env`.

To try it without voice, run `python -m saarthi.sim` for a text chat as the driver or owner. To run the tests, use `pytest` (they run offline with a scripted LLM and Pipecat's test harness).

## Accuracy evaluation (Prisma in real noise)
`eval/run_eval.py` measures word error rate and **amount accuracy** for each noise condition (fuel pump, moving cab, highway roadside, phone speaker), **with and without** the domain keyterms list (`data/keyterms.txt`, which includes Rajasthani place names). Recordings are made with the speakers' consent, stay private, and are never committed; only the aggregated results are published.

Results will be filled in after the pilot.

## Data sources
- **Toll plazas:** NHAI Toll Information System, via the community snapshot [geohacker/toll-plazas-india](https://github.com/geohacker/toll-plazas-india) (30-06-2020, Government Open Data License – India). Only name, location and fee per vehicle class are kept. Rates are from 2019–20, so actual tolls today are higher: treat them as a minimum, and treat the FASTag or driver amount as the truth.
- **Diesel price:** no free daily government API exists (PPAC data on data.gov.in is periodic), so the owner sets the price and the CEO Agent uses it.
- **Freight rates:** the government doesn't publish lane-wise freight rates. Benchmarks will come from Saarthi's own trip data (see `docs/ROADMAP.md`).

## Honest limitations
- The reasoning step uses OpenAI-compatible LLMs with ordered failover (`LLM_*`, then `LLM2_*`), for example a Gnani-provided LLM or a self-hosted Evon first and Gemini as the fallback. Speech is always Gnani.
- The web app needs the screen awake (dimmed) in the dashboard holder. Listening with the screen off, and dialing without a tap, need the planned Android wrapper.
- Always-on listening only sends detected speech to Prisma, but credit use per hour still has to be measured in the pilot.
- Without a Google Maps key, routes come from OSRM and have **no live traffic**. The agent says so rather than guessing.
- Nearby-mechanic data comes from OpenStreetMap and is incomplete in rural areas. Fleet Mesh is the fallback.
- Owner alerts on Telegram are voice notes plus a one-tap button to open the driver's chat or call (bots can't place phone calls).
- All fleets, plates and people in this repo and in the demo are **synthetic**.

## Project layout
`saarthi/companion.py` live voice pipeline (Pipecat + pipecat-gnani) · `static/driver.html` phone app · `static/office.html` owner dashboard · `agents/driver.py` Driver Agent · `agents/ceo.py` CEO Agent · `agents/mesh.py` Fleet Mesh · `costing.py` cost engine · `speech.py` Gnani Prisma/Timbre · `safety.py` escalation rules · `fatigue.py` check-ins + live location · `web.py` tracking page + n8n API · `telegram_app.py` bots.

See `docs/ROADMAP.md` for satellite and open data, prediction models, and training on real data.
