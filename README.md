# Saarthi: a voice co-driver and AI office for small Indian fleets

> Built for Gnani AI's **Great Indian AI Internship Challenge** (solo entry).
> **Gnani models used:** **Prisma v3.0** (speech-to-text) for every voice note, and **Timbre v2.5** (text-to-speech) for every spoken reply and alert.

75% of Indian truck operators own fewer than 5 trucks. They run on phone calls, paper diaries and guesswork, and can't afford GPS hardware or fleet software. Saarthi gives every truck a **live voice companion**: the phone sits in a holder on the dashboard, and the driver just talks, never touching it. The office gets a **CEO Agent** that oversees all the trucks and keeps the owner updated.

## Try it

| What | Link |
|---|---|
| 🎬 **Film mode**: the full simulated Udaipur → Hyderabad trip, auto-played with Gnani voices | https://saarthi-fleet.onrender.com/film |
| 🏢 Owner's office (CEO Agent, live map, demo trip panel, CEO voice call) | `https://saarthi-fleet.onrender.com/office?k=<office key>` (key shared with the submission) |
| 🚚 Driver companion (live voice, Hindi + 9 more languages) | from `https://saarthi-fleet.onrender.com/links?k=<office key>` |
| 💻 Code | https://github.com/meetsaxena182006-cell/gnani-ai- |

Hosted on Render's free plan (Singapore): the first visit after it has been idle takes ~1 minute to wake up, and live voice
is slower there than on a laptop. Film mode is pre-voiced, so it plays smoothly anywhere. If the server restarted, the film page
shows "Prepare film again" (office key, ~4 min). **Everything in the demo trip is simulated demo data.**

**Film mode covers:** trip assignment by the CEO Agent, route plan (NHAI toll plazas, diesel, rest stops, ETA), live location
on the map, FASTag toll auto-logging, fuel stop + diesel reported by voice (live AI confirms and saves it), traffic jam re-route,
heavy-rain warning, fatigue rest at a dhaba, tyre burst → breakdown flow → agent-to-agent help from another fleet's truck,
repair cost logging, papers / PUC renewal before a border check, client ETA call, language switch to Telugu, delivery + POD + trip P&L, the owner asking the CEO Agent for profit
(live AI answer), and the **empty-truck agent**: after unloading it finds another fleet's urgent overflow load nearby,
checks the truck's papers and payload and the poster's legitimacy (KYC, GSTIN, track record, overdue payments), rejects
unsafe offers and books the best safe one, so the truck never returns empty.

**Live data:** OpenStreetMap / OSRM routes, Open-Meteo weather, Overpass (mechanics, fuel, dhabas), NHAI toll plazas.
Live traffic: set `TOMTOM_API_KEY` (free tier, 2,500 requests/day) or `GOOGLE_MAPS_API_KEY`.

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
- **CEO Sentinel (`sentinel.py`): proof that nothing is missing.** Every minute each truck gets a checklist: location live, Driver Agent reachable, open incidents, driving hours, check-ins answered, every crossed toll accounted for, diesel logged within one tank's range (odometer-based), unconfirmed entries, freight set, e-way bill validity, documents. The office shows one headline ("✅ Sab theek" or what exactly needs attention) and the owner can ask "sab theek hai? kuch miss to nahi?".
- **The loop is closed:** a toll question the driver didn't answer is asked again after 30 min ("ek baat reh gayi — kitna laga tha?"); still nothing after the second reminder → the owner is told with the plazas and expected amount. 4.5 h+ continuous driving → the companion suggests chai and rest (never to speed up). An expired e-way bill or document on an active trip → owner, once a day.
- Also catches unexplained long stops (asks the driver if all is well), heading away from the destination, overspeeding (gentle warning), trucks gone offline (owner alerted), and diesel that doesn't add up (km driven vs litres logged).

**Load & route planning (precise, optimised)**
- **Trip planner:** for every route option × every departure time in the next 24 h, it simulates the drive in 5 km steps:
  - truck speed, slowed by the **forecast weather at that stretch at that hour** (fog, heavy rain, wind);
  - **live traffic** (with a Google key) for leaving now;
  - **driver rules** from the Motor Transport Workers Act 1961 (via MV Act s.91): a 30-min rest within 5 h, 8 h driving a day, 10 h spread-over, 8 h sleep; 16 h driving if there is a relief driver;
  - a **health break** every 2 h, with **night driving** penalised;
  - **fuel stops** before the tank reaches 15%;
  - **city no-entry windows** (`data/truck_restrictions.json`).
  Each plan is scored in ₹: freight − diesel − **NHAI tolls on that route** − maintenance − driver/fixed cost per hour − risk. You get the best departure, a stop schedule with **named dhabas and pumps** (OpenStreetMap), ETA, profit, and *why* (e.g. "leave at 05:30: 4.5 h less night driving, 2 h less fog").
- **Fleet load planner:** every truck (where and when it becomes free) × every open load (own orders + Saarthi network). It counts empty km, checks capacity, and finds the **optimal assignment (Hungarian algorithm)** for maximum total profit, then **consolidates part-loads** into spare capacity. Reports km utilisation and idle trucks.
- **Updated to the CEO all the time:** the plan goes to the owner (full) and the driver (spoken summary) at trip start. The companion announces each planned stop as the truck approaches it ("10 km aage Demo Dhaba pe chai break plan hai"). If the truck falls 1 h+ behind, the owner gets a new ETA. **The driver is never pushed to speed up to recover time.**

**Business side: money owners lose today**
1. **Detention:** GPS records arrival and departure at the loading and unloading points. Waiting beyond the free hours (default 4 h) becomes a ₹/day claim (default ₹1,500/day, part days count), with timestamps as proof, and is added to the invoice automatically. The driver can also say "pahunch gaya / nikal gaye".
2. **Payments:** "maal utar gaya" → the phone shows a POD camera button → the invoice is generated (freight + detention − advance) → due date → payment reminders drafted for the owner to forward → "kis party se kitna baaki" → the owner says "Sharma ji se 20,000 aa gaye" to record payments.
3. **Driver ledger:** salary monthly, bata per trip day, advances (from what the driver says) and payments. The driver can ask "mera kitna baaki hai?" by voice, and the owner gets a salary-day reminder.
4. **E-way bill:** validity (1 day per 200 km, to midnight) is compared with the ETA. Before it lapses, the owner and driver are warned so it can be extended (8 h window).
5. **Service and tyres by km:** an odometer from GPS distance → reminders before service is due.
6. **E-challans:** pending challans per truck, entered manually or pulled from a licensed provider API (`CHALLAN_API_URL`), with a weekly owner alert.
7. **Return loads (load mesh):** owners post loads they can't carry. When a truck nears its destination, the CEO Agent finds loads starting nearby, with estimated profit from the cost engine, and the owner says "load #ID le lo" to take one.

**Companion brain**
- Gets fresh **situation awareness** every turn: time, speed, hours driving, nearest landmark, unreported tolls, last diesel fill.
- A fast model handles conversation, and `think_deeply` sends hard questions ("raat tak pahunchenge?", "kahan rukein?") to a stronger reasoning model along with the truck's real data.
- **Road analysis:** route options, truck-adjusted ETA, NHAI tolls on the way with expected cost, weather expected at each stretch *when the truck gets there*, and night-driving hours.
- **Remembers him:** personal things the driver shares (children's names, village, favourite singer, an upcoming wedding) are saved as short facts (`remember` tool, last 30 kept) and brought up naturally on later days, like a friend would.
- Reasons in four silent steps before answering: understand his need and mood → check facts with tools → connect them (fog ahead + hours driven → stop at the next dhaba) → one clear suggestion with the reason.
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

## Driver app (Android)
`android/` is a native app that keeps the companion alive with the **screen off**. It provides:
- native GPS;
- voice call handling ("Sharma ji ka call aa raha hai, uthaun?" → *haan* answers on speaker);
- dialing help after the driver says *haan*;
- WhatsApp/SMS messages read aloud;
- FASTag debit SMS sent automatically for toll reconciliation;
- songs opening straight in YouTube Music.

GitHub Actions builds the APK on every push. See `android/README.md`.

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

**One link to review everything, no API keys needed:** double-click `review_demo.bat` (or `python scripts/review_demo.py`). It prints a public `https://…trycloudflare.com/review?k=…` link (also saved in `data/review_link.txt`). Left: the CEO office. Right: what the driver's companion says in the cab and what the owner receives. A synthetic truck drives Udaipur → Ahmedabad on NH48, and you watch the CEO notice each NHAI toll, ask the driver, remind him, then alert the owner (reminder sped up to 2 min for review). Uses its own `data/review_demo.db`; live voice and AI chat still need the keys (`start_public.bat`). The link works while that window stays open.

To review without trucks: `python -m saarthi.demo_story`, then `python -m saarthi.main` and open `/office`. This loads a synthetic day: tolls, detention, an overdue invoice, service due, a challan and a return load.
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
`saarthi/companion.py` live voice pipeline (Pipecat + pipecat-gnani) · `static/driver.html` phone app · `static/office.html` owner dashboard · `agents/driver.py` Driver Agent · `agents/ceo.py` CEO Agent · `agents/mesh.py` Fleet Mesh · `costing.py` cost engine · `speech.py` Gnani Prisma/Timbre · `safety.py` escalation rules · `sentinel.py` CEO checklist + follow-ups · `fatigue.py` check-ins + live location · `web.py` tracking page + n8n API · `telegram_app.py` bots.

See `docs/ROADMAP.md` for satellite and open data, prediction models, and training on real data.
