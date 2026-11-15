🚚 75% of Indian truck owners run fewer than 5 trucks — on phone calls, paper diaries and guesswork.

So I built **Saarthi (सारथी)** for the Great Indian AI Internship Challenge by @Gnani.ai — two voice AI agents that run a small fleet while the owner and driver just talk:

🧠 **CEO Agent (owner's office)** — a voice-first, Jarvis-style partner: no forms, just a glowing orb that speaks first in Hindi: "आज का क्या प्लान है? कोई नई ट्रिप लगानी है?" The owner just says "उदयपुर से हरिद्वार, रमेश की गाड़ी भेजो" and it plans everything end to end: road route, breaks, diesel, tolls, weather, ETA and profit, then hands it to the driver's agent. Orders, trucks and driver messages work the same way.
🎙️ **Driver Agent (on the driver's phone)** — a hands-free co-driver in Hindi + 9 Indian languages: today's plan, mileage and diesel-saving tips, breakdown help from nearby trucks, a return load when the truck is empty, expenses logged by voice.
⚡ The two agents update each other in under a second. Driver says "tyre burst" → owner's office gets the alert with location and nearest help in 0.7 s.

🧠 Models used
• Gnani **Prisma v3.0** — speech-to-text (everything the owner and driver say)
• Gnani **Timbre v2.5** — text-to-speech (every spoken reply)
• Reasoning: **gpt-oss-120b** (via Groq), with **NVIDIA Nemotron** as backup
• Maps & data: OpenStreetMap / OSRM routes, Esri map tiles, Open-Meteo + MET Norway weather, NHAI toll plaza list

🎬 Watch a full simulated trip, auto-played with Gnani voices: https://saarthi-fleet.onrender.com/film
💻 Code: https://github.com/meetsaxena182006-cell/gnani-ai-

📝 Honest note: all fleets, drivers and trips in the demo are synthetic, phone calls are simulated, and the "drive" uses simulated GPS on real roads. Maps and routes come from free OpenStreetMap services, so there is no live traffic yet. Live Google Maps traffic, real phone calls (WhatsApp Business / telephony) and FASTag/vehicle APIs switch on by adding their paid keys — the code is already wired for them.

Would love your feedback — especially if you know someone in trucking! 🙏

#GnaniAI #GreatIndianAIInternshipChallenge #VoiceAI #AIAgents #Logistics #Trucking #IndianLanguages #BuildInPublic
