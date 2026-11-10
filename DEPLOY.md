# Deploy Saarthi on Render + Cloudflare (free, permanent links — best for the video and live tests)

**What you get:** one permanent address like `https://saarthi-fleet.onrender.com` with
- **CEO link:** `/office?k=<office key>` — live map, CEO Sentinel, chat with the CEO Agent, every driver conversation;
- **one Driver Agent link per truck:** `/driver?v=MW-T1&k=…` — voice (tap Start) or typing, live GPS;
- **all links on one page:** `/links?k=<office key>`.

Render runs the app; **Cloudflare TURN** carries the phone voice (Render has no UDP, so voice needs the relay).

### Step 1 — Keys (10 min, once)
| Key | Where | Needed for |
|---|---|---|
| `LLM_API_KEY` | https://aistudio.google.com/apikey → *Create API key* (free Gemini) | the agents think and talk (chat + voice) |
| `GNANI_API_KEY` | your Gnani dashboard | live voice: Prisma v3.0 (speech-to-text), Timbre v2.5 (voice) |
| `CF_TURN_KEY_ID`, `CF_TURN_API_TOKEN` | Cloudflare → Realtime → TURN Server → Create (see below) | voice on mobile data |
| `GOOGLE_MAPS_API_KEY` *(optional)* | Google Cloud → Routes API | live traffic on routes |

### Step 2 — Put the code on GitHub (once)
Render deploys from a GitHub repository. Create an **empty private** repo on https://github.com/new (name `saarthi-fleet-agent`, no README), copy its URL, and give it to Claude; Claude pushes the code (Windows asks you to sign in to GitHub once).

### Step 3 — Create the service on Render (5 min)
1. https://dashboard.render.com → sign in with GitHub.
2. **New + → Blueprint** → pick `saarthi-fleet-agent` → it reads `render.yaml`.
3. It asks for the secret keys from Step 1: paste each one (leave `GOOGLE_MAPS_API_KEY` empty if you don't have it) → **Apply**.
4. Wait ~5 min for the first build. Then open **Environment** and copy the value of `API_SHARED_SECRET` — that's your office key.
5. Open `https://<your-service>.onrender.com/links?k=<office key>` — the ✅/❌ line at the top shows which keys are working.

**Free-plan notes:** the site sleeps after 15 min with nobody on it; the first open then takes ~1 minute (open the office link a minute before recording). The demo data is rebuilt on each restart; the links never change.

### For the video
- A **SIMULATED** demo truck (RJ00 DEMO 03, "Kishan (demo)") drives NH48 Udaipur → Ahmedabad by itself and crosses the real NHAI plazas at Paduna and Khandi Obari. Open its driver link to play the driver: the CEO Agent asks "kitna toll laga?", re-asks after 3 minutes, then alerts the owner.
- For the **real test driver**, give him the MW-T1 or MW-T2 link: his phone's GPS moves that truck on the CEO's map.

---

# Alternative: Azure for Students (free, always on)

**Why Azure for Students:** a real Linux server that stays on 24/7. The CEO checks run every minute, the data is kept, and phone voice works directly. It comes with $100 credit, **no card**, plus 750 free server hours every month. When the credit ends, the server simply stops; nothing is ever charged to you.

Time: about 20 minutes the first time.

---

## Part 1: Get Azure for Students (5 min, once)
1. Open **https://azure.microsoft.com/free/students** and click **Start free**.
2. Sign in, then verify with your **college email** (a code is sent to it).
3. When it says your subscription is ready, open **https://portal.azure.com**.

## Part 2: Create the server (10 min, once)
1. In the top search bar type **Virtual machines**, open it, click **+ Create → Azure virtual machine**.
2. Fill in the **Basics** tab:
   - **Subscription:** Azure for Students
   - **Resource group:** click *Create new*, type `saarthi`
   - **Virtual machine name:** `saarthi`
   - **Region:** `(Asia Pacific) Central India`
   - **Image:** `Ubuntu Server 24.04 LTS - x64 Gen2`
   - **Size:** click *See all sizes* and pick **B2ats_v2** (or **B1s**). Pick one marked *Free services eligible*. Don't pick an Arm size (names with "p").
   - **Authentication type:** SSH public key · **Username:** `azureuser` · **Key pair name:** `saarthi-key`
   - **Public inbound ports:** Allow selected ports → tick **SSH (22)**, **HTTP (80)**, **HTTPS (443)**
3. **Disks** tab: OS disk type **Premium SSD**, size **64 GiB (P6)**. Two P6 disks are in the free list.
4. Click **Review + create → Create**. A box pops up: click **Download private key and create resource**. It saves `saarthi-key.pem` in Downloads. **Keep this file safe; it's the only way in.**
5. When it says *Your deployment is complete*, click **Go to resource**.
6. Give it a name on the internet: next to **DNS name** click *Not configured* → **DNS name label**: type something like `saarthi-meet` → **Save**.
   Your address is now `saarthi-meet.centralindia.cloudapp.azure.com` (shown on the same page).
7. For live phone voice: left menu **Networking → Network settings → Create port rule → Inbound port rule** → Protocol **UDP**, Destination port ranges **`1024-65535`**, Name `webrtc-voice` → **Add**.

## Part 3: Put Saarthi on it (one double-click, 5–10 min)
1. Optional, for the real AI and voice: put your keys in `.env` in the project folder (copy `.env.example` to `.env`, fill in `GNANI_API_KEY`, `LLM_API_KEY`, and set `API_SHARED_SECRET` to any password).
   **Without keys, the site runs in review mode** with synthetic demo trucks, the CEO office and both phones.
2. Double-click **`deploy_azure.bat`**.
3. Paste your DNS address (from Part 2, step 6) and the path to `saarthi-key.pem`, then press Enter.
   The first time, Windows asks *Are you sure you want to continue connecting?*; the script answers it for you.
4. At the end the live link opens in your browser and is saved in `data\live_link.txt`.

**To update the live site after any change:** double-click `deploy_azure.bat` again. The database is kept.

## Optional: Cloudflare TURN (makes phone voice connect on any network, free)
Phones on Jio/Airtel data, and laptops behind home Wi-Fi, often can't connect a voice call directly. A TURN relay fixes that.
1. Sign in at **https://dash.cloudflare.com** (free account).
2. Left menu: **Realtime → TURN Server → Create** (name it `saarthi`).
3. It shows a **Turn Token ID** and an **API Token**. Copy both into `.env` yourself:
   `CF_TURN_KEY_ID=<Turn Token ID>` and `CF_TURN_API_TOKEN=<API Token>`
4. Run the deploy again (or restart the app). Saarthi creates short-lived passwords from this key every day; the key itself never reaches the phone.
Free: the first 1,000 GB of relay traffic each month.

## Checking it
- Review mode: `https://<your-address>/review?k=<key>` shows the CEO office on the left and the driver's phone + owner's phone on the right, with a demo truck driving NH48.
- Full mode: `https://<your-address>/office` (office key = `API_SHARED_SECRET`) and one `/driver?v=…&k=…` link per truck. Open a driver link on a phone, tap Start.
- If HTTPS shows an error in the first 2 minutes, wait: the free certificate (Let's Encrypt via Caddy) is being created.

## If something goes wrong
- *Copy failed*: check the DNS address, the key path, and that port 22 is allowed (Part 2, step 2).
- *Setup failed*: the script prints the last log lines; send them to me.
- See logs any time: `ssh -i saarthi-key.pem azureuser@<your-address> "sudo journalctl -u saarthi -n 100"`

## Cost safety
- The B1s/B2ats_v2 server and two 64 GB disks are in Azure's free services list (750 hours each per month).
- Small extras (the public IP address, network traffic) come out of the $100 student credit, never your money. No card means Azure can't bill you; when the credit ends, the server is switched off.
- To stop everything: portal → Resource groups → `saarthi` → **Delete resource group**.
