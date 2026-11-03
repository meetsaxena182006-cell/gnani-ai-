# After the challenge: maps, predictions, and training on real data

## 1. Satellite and map data (open or free)

| Source | What it gives | Use in Saarthi | Caveat |
|---|---|---|---|
| **OpenStreetMap** | Roads, dhabas, fuel pumps, mechanics, toll booths | Routing graph, nearby help (already used) | Rural coverage is patchy, so drivers should be encouraged to add places |
| **Esri World Imagery** (tiles) | High-resolution satellite base map | Satellite toggle on the office map (already added) | Free with attribution, but **not open data**; terms limit heavy or commercial use |
| **ISRO Bhuvan** (WMS) | Indian satellite imagery and thematic layers | "Sovereign" Indian base layer | Registration; historically strict tile and usage limits; check current terms |
| **Sentinel-2** (ESA Copernicus) | Free 10 m optical imagery every ~5 days | Yard and parking mapping, road-change detection | Too coarse and too slow for live traffic |
| **Sentinel-1 SAR** (Copernicus) | Radar that sees through clouds, day and night | **Monsoon flood extent over highways** (overlay on OSM roads) → reroute warnings | Needs processing (e.g. Google Earth Engine); days of latency |
| **INSAT-3D fog product** (MOSDAC, ISRO) | Fog maps every 30 min at 4 km over India | **Winter fog warnings on north-India highways** | Research-only licence, registration needed; can't separate fog from low cloud |
| **Open-Meteo** | Forecasts (rain, visibility, wind, heat) | Weather along the route (already used) | Model forecast, not observation |

**Honest take:** satellites are too slow and too coarse for live traffic. They are most useful for **monsoon flooding** (Sentinel-1) and **winter fog** (INSAT-3D). Live traffic in India still needs Google/MapmyIndia, or our own fleet's GPS traces once enough trucks are on Saarthi.

## 2. Better routing, prediction and costing with open source

| Tool | Gives | Why it matters for trucks |
|---|---|---|
| **Valhalla** (self-host, OSM) | Truck routing with **height, weight and width limits** set per request, plus toll avoidance | Avoids low bridges and no-entry roads for trucks; OSRM can't do this per request |
| **GraphHopper** (self-host) | Fast routing with truck profiles | Alternative to Valhalla |
| **OSM toll tags + NHAI fee-plaza list** | Toll plaza locations and rates | Exact toll cost per route instead of the current per-km estimate |
| **Our own data** (most valuable) | Actual km/L per truck, per route, per load; real toll and driver costs; actual vs estimated trip time | A cost model that learns each owner's real costs |

Prediction models to train once data exists (LightGBM/XGBoost are enough, no deep learning needed):
- **Trip cost and ETA**: features are distance, route, vehicle, load, season, time of day and driver; target is actual cost and time.
- **Mileage per vehicle**: catches a falling km/L early, which signals maintenance needs or **diesel pilferage** (logged litres vs GPS distance). This is a big pain point for owners.
- **Freight rate benchmark**: what a lane actually paid recently, so the CEO Agent can say "this offer is 12% below market".
- **Breakdown risk**: from repair history and km, predicts tyre or clutch failures before a trip.

## 3. Training on real data: a "data flywheel"

**Collect, with consent (DPDP Act 2023):**
- An explicit opt-in from each driver and owner, in their language, stating the purpose. Data stays in India.
- Audio is kept only with consent and has a retention limit. Phone numbers, names and plates are pseudonymised before training.

**Free labels the product already produces:**
- Every read-back ("diesel ₹4,500, sahi hai?") followed by *haan* or *nahi* is a **labelled speech sample**: audio → correct amount. A *nahi* plus the correction marks an ASR error to learn from.
- Corrected transcripts, confirmed expenses, and resolved incidents with their outcomes.
- Tool-call traces: what the driver said → which tool was right.

**What to train, in order:**
1. **Keyterms and regional vocabulary (now):** grow `data/keyterms.txt` per route and region from real transcripts. This is cheap and gives immediate gains.
2. **ASR for dialects:** with enough consented Marwari/Mewari, Bundeli and Haryanvi audio paired with corrected text, work with Gnani on adaptation, or fine-tune open Indic ASR models (e.g. AI4Bharat IndicConformer) for dialects Prisma doesn't cover yet. Measure word error rate per dialect and noise condition (the `eval/` harness).
3. **Reasoning model:** fine-tune **Evon v3.3** (open weights, Apache-2.0) with LoRA on anonymised tool-calling traces. That gives a sovereign, cheaper model that knows trucking language and tools. Before rollout, gate it on a held-out test set (amount accuracy, correct escalation, no invented numbers).
4. **Prediction models** (section 2) on trip and expense data.

**Engineering:**
- Event log → nightly anonymisation → a labelled dataset with versions → training → **evaluation gates** → shadow mode (the new model runs alongside the old one and is compared) → gradual rollout.
- Safety rules (escalation, money confirmation, dial whitelist) **stay in code** and are never learned. Models only get better at understanding and suggesting.
