# Saarthi — a live voice companion for every truck, an AI CEO for every fleet

**Gnani models:** Prisma v3.0 (speech-to-text: auto language detection, keyterms, number normalisation) and Timbre v2.5 (text-to-speech, a voice for each language).

## The idea
- **Driver Agent = a companion living on the driver's phone.** The phone sits in a holder on the dashboard. The companion is always on and hands-free: the driver never touches the phone and simply talks. It chats like a friend, checks in every 1–2 hours ("Ram Ram boss, sab theek? tabiyat badhiya?"), keeps the driver alert, logs money, and handles breakdowns.
- **Fleet Mesh.** When a truck is in trouble, its companion asks every Saarthi companion on the same road. Each nearby companion asks its own driver: "aage ek truck mushkil mein hai, madad kar sakte hain?" The first driver to say *haan* is matched, everyone else hears "madad mil gayi", and when the problem is solved everyone is told.
- **CEO Agent = always live on the server.** The owner can reach it through the laptop dashboard (`/office`) or Telegram voice notes. It receives every driver event, keeps the owner updated, hands emergencies straight to the owner, and gives precise deal costing (freight − diesel − driver − tolls − maintenance = profit).

```
Phone in dashboard holder (web app; screen kept awake)          Owner laptop /office · Telegram voice notes
  mic+speaker ⇄ WebRTC ⇄ [VAD → Prisma → Guard → LLM+tools → Timbre]  ⇄  CEO Agent (server)
  GPS over data channel ─────────────────────────┘        │
                         Fleet Mesh ⇄ other trucks' companions on the same road
```

## How "always on, no touching" works
| Need | Implementation |
|---|---|
| Always listening without burning credits | Silero voice-activity detection on the server cuts out speech segments, and **only speech** goes to Prisma (REST, under 30 s per segment). |
| Doesn't interrupt family phone calls or chats with the helper | The guard responds only when addressed ("Saarthi …"), during an open conversation (90 s), or when emergency/breakdown words are heard. |
| Talks first, every 1–2 hours | Check-in scheduler (`FATIGUE_INTERVAL_MIN`, default 90). Two unanswered check-ins alert the owner. |
| Many languages | Prisma `indic-auto` detects the language → the companion answers in it → Timbre switches to that language's voice. Covers Hindi, Punjabi, Gujarati, Marathi, Bengali, Tamil, Telugu, Kannada, Malayalam and English. **Marwari/Mewari is not a Prisma language:** it is recognised as Hindi with Rajasthani keyterms, and the companion replies in simple Hindi with a Rajasthani touch. |
| Calls the nearest help | A breakdown triggers a nearby mechanic lookup → "Unko call lagaun?" → *haan* → the phone dials. Only numbers that came from a lookup (or 112/108/1033) can be dialled. The web app opens the dialer pre-filled; one tap is needed until the Android wrapper is built. |
| Emergency | Rule-based: the owner is alerted immediately, and the driver is asked "112 lagaun?". The AI never talks to emergency services. |
| Money | The amount is read back, and the guard saves it only after *haan*. The LLM never does the arithmetic. |
| Weak highway network | Auto-reconnect with backoff. TTS uses per-reply HTTP streaming (SSE), so a network drop doesn't silence the companion. A TURN server is configurable for mobile networks. |
| Reasoning LLM | Ordered failover: `LLM_*` (e.g. a Gnani-provided LLM or self-hosted Evon), then `LLM2_*` (Gemini/Groq). |

## Done (tested offline: 23 tests, plus a WebRTC end-to-end smoke test)
Live companion pipeline · phone web app · Fleet Mesh with voice consent and solved/closed broadcasts · CEO Agent · office dashboard with map/satellite toggle · Telegram for owner voice notes · deterministic cost engine · fatigue check-ins · client tracking link · n8n workflows · Prisma accuracy harness.

## Next (in order)
1. **On the laptop with real keys:** test Prisma/Timbre quality and latency, check credit cost per minute, and choose the voices.
2. Test the phone on Jio/Airtel 4G on a highway; add a TURN server if WebRTC fails behind mobile NAT.
3. Pilot with 3–5 drivers who consent; record the accuracy evaluation per noise condition.
4. Make the posts and the 60-second video (diesel log → tyre burst → mesh help → owner alert → solved).
5. Stretch goal: an Android wrapper (foreground service, screen-off listening, direct dialing).
