# Saarthi (सारथी) — one-page solution pitch

**Great Indian AI Internship Challenge · built on Gnani Prisma v3.0 (STT) + Timbre v2.5 (TTS)**

## Problem
75% of Indian truck owners run fewer than 5 trucks. They manage trips, diesel, tolls, breakdowns, papers and payments over phone calls and paper diaries. Drivers drive 12+ hours, can't type while driving, and mostly speak Hindi or a regional language. Money leaks through diesel theft, empty return trips, missed paper renewals and slow breakdown help.

## Who uses it
- **The owner** (small fleet, 1–5 trucks) — in the office or on the phone.
- **The driver** — hands-free, phone in the holder, in his own language.

## Solution: two voice AI agents that work together
**CEO Agent (owner) — a voice-first, Jarvis-style partner.** It speaks first in Hindi ("आज का क्या प्लान है?"). The owner just talks:
- "Udaipur se Jaipur trip lagao, Raju ki gaadi" → plans the full trip (road route, tolls, diesel, rest stops, weather, ETA, profit) and sends it to the driver's agent.
- "Naya order aaya hai…" → saves the order with a cost/profit quote. "Raju ko bolo…" → spoken aloud in the cab.
- Live map of every truck, paper-expiry warnings, monthly profit & loss.

**Driver Agent (driver) — a hands-free co-driver, Hindi + 9 Indian languages.**
- Today's plan, diesel/expenses by voice, FASTag tolls logged automatically.
- Traffic reroute, rain and fatigue warnings, mileage + driving score with diesel-saving tips.
- Breakdown → owner alerted with location; a nearby truck from another fleet is asked to help (agent-to-agent).
- Empty truck → finds a return load from other fleets after checking papers and the party.

The agents update each other in under a second; common questions are answered by code in < 1 s, the rest by the LLM.

## Gnani AI technology
- **Prisma v3.0** — streaming speech-to-text with Indic auto language detection (owner and driver).
- **Timbre v2.5** — text-to-speech for every spoken reply (Hindi, Telugu, more).
- Reasoning: gpt-oss-120b via Groq (NVIDIA Nemotron backup). Maps/data: OpenStreetMap/OSRM, Open-Meteo/MET Norway, NHAI toll list.

## Try it
- **Test kit for judges (fresh empty fleet + driver app, no demo data):** link in the submission form.
- **Demo film:** https://saarthi-fleet.onrender.com/film
- **Code:** https://github.com/meetsaxena182006-cell/gnani-ai-

## Impact & next steps
Saves diesel (mileage coaching, theft checks), cuts empty return trips, keeps papers valid, gets help faster after a breakdown — with zero typing. Next: WhatsApp Business + telephony for real calls, Google Maps live traffic, FASTag/Vahan APIs (code already wired; needs paid keys).

*Honest note: demo fleets, trips and calls are synthetic; the drive uses simulated GPS on real roads.*
