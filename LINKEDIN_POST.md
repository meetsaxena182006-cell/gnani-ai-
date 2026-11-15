🚚 75% of Indian truck owners run fewer than 5 trucks — on phone calls, paper diaries and guesswork.

So I built **Saarthi (सारथी)** for the Great Indian AI Internship Challenge by @Gnani.ai — two voice AI agents that run a small fleet while the owner and driver just talk. No forms, no typing.

🧠 **CEO Agent — the owner's voice-first partner (like Jarvis)**
Open the office and a glowing orb speaks first in Hindi: "आज का क्या प्लान है? कोई नई ट्रिप लगानी है?" Then just talk:
• "उदयपुर से हैदराबाद, रमेश की गाड़ी भेजो" → plans the whole trip end to end: road route, tolls, diesel, rest stops, weather, ETA and profit, then hands it to the driver's agent
• "नया ऑर्डर आया है — हैदराबाद से नागपुर, 18 टन" → saves the order with a cost and profit quote
• "रमेश को बोलो आराम से चलाए" → the message is spoken aloud in the truck cab
• Adds trucks, tracks every truck on a live map, warns about expiring papers, gives the month's profit and loss

🎙️ **Driver Agent — a hands-free co-driver on the driver's phone (Hindi + 9 Indian languages)**
• "आज का प्लान बताओ" → today's route, breaks and ETA
• Diesel and expenses logged by voice, FASTag tolls logged automatically
• Traffic jam → reroute, heavy rain → slow-down warning, 4+ hours of driving → rest-stop reminder
• "माइलेज कैसा है?" → mileage, driving score and diesel-saving tips
• Tyre burst → the owner is alerted with location, and a nearby truck from another fleet comes to help (agent-to-agent)
• Empty truck after delivery → finds a return load from another fleet, after checking papers and the party

⚡ The two agents update each other in under a second.

🧠 Models used
• Gnani **Prisma v3.0** — speech-to-text (everything the owner and driver say)
• Gnani **Timbre v2.5** — text-to-speech (every spoken reply, in Hindi and Telugu)
• Reasoning: **gpt-oss-120b** (via Groq), with **NVIDIA Nemotron** as backup
• Maps & data: OpenStreetMap / OSRM routes, Esri map tiles, Open-Meteo + MET Norway weather, NHAI toll plaza list

🎬 Watch a full trip (Udaipur → Hyderabad) with every feature, auto-played with Gnani voices: https://saarthi-fleet.onrender.com/film
💻 Code: https://github.com/meetsaxena182006-cell/gnani-ai-

📝 Honest note: all fleets, drivers and trips in the demo are synthetic, phone calls are simulated, and the "drive" uses simulated GPS on real roads. Maps and routes come from free OpenStreetMap services, so there is no live traffic yet. Live Google Maps traffic, real phone calls (WhatsApp Business / telephony) and FASTag/vehicle APIs switch on by adding their paid keys — the code is already wired for them.

Would love your feedback — especially if you know someone in trucking! 🙏

#GnaniAI #GreatIndianAIInternshipChallenge #VoiceAI #AIAgents #Logistics #Trucking #IndianLanguages #BuildInPublic
