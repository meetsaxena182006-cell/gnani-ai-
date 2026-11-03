# Gnani AI Challenge — Rules Compliance Sheet

Source: official pages (gnani.ai/internship, challenge blog, registration form) as surfaced by
web search on 2 Nov 2026, plus the user's own research notes. gnani.ai itself is blocked from
the cloud build container, so re-verify anything marked [VERIFY] on the official page.

## Hard rules (breaking these = risk of disqualification)
1. **Eligibility:** enrolled at an Indian college/university, 18+ on registration day. Solo entry (Meet Saxena).
2. **One entry per participant.** Submit once. Use the credits/API key from ONE account only.
3. **Must use Gnani models/APIs**, and the submission must NAME each model used
   (Prisma STT, Timbre TTS; Evon v3.3 if used).
4. **Scope:** API platform at app.gnani.ai/voice. Agent builder is out of scope.
5. **Synthetic data only:** no real phone numbers, account numbers, Aadhaar, PAN, or real
   recorded calls in the entry. Allowed: sample scripts, self-recorded audio WITH CONSENT,
   generated fake identifiers.
6. **No** bought engagement, bots, paid promotion, engagement swaps, plagiarism, misleading demos.
   => demo video must show what the app ACTUALLY does; label anything simulated.
7. **Deadline:** 11:59 PM IST, 10 Nov 2026. No extensions. Target: submit 9-10 Nov early.

## Submission (via programme page)
- [ ] Demo / video of the working app (60 s cut also targets "Demo Day Drop") [VERIFY length]
- [ ] Written project explanation
- [ ] Gnani models/APIs used (named)
- [ ] Post links: ONE nominated post per platform (LinkedIn and/or X)

## Social posts
- Explain what was built + which Gnani tech is used
- Tag Gnani AI's official account
- Include **#GnaniAI** and **#GreatIndianAIInternshipChallenge**
- Tagging alone does NOT complete the entry — the form must be submitted.
- Engagement (reactions, comments, reshares) on nominated posts is verified and used for shortlisting.

## Selection
- Engagement verification -> submission review -> shortlist -> individual interview.
- Only a written offer confirms an internship. Awards are judged separately and do not affect offers.
- Max one award per person/team. Gnani may change rules/timelines at its discretion.

## GitHub / code
- A public repo is NOT an official requirement [VERIFY], but it strengthens the write-up and interview.
- NEVER commit API keys: `.env` is gitignored; only `.env.example` with placeholders.
- No real driver phone numbers / names / recordings in the public repo — synthetic fixtures only.
- Consented real recordings used for accuracy measurement stay PRIVATE (not committed);
  publish only aggregated accuracy numbers.
- Evon v3.3 weights are Apache-2.0 on Hugging Face (gated). Any third-party LLM (Gemini/Groq):
  allowed? [VERIFY on Discord] — disclose it clearly in the write-up either way.

## Open questions for Gnani Discord (https://discord.gg/aECcvtABpq)
1. Is a non-Gnani LLM OK for reasoning if Prisma + Timbre do all speech?
2. API rate limit (1 concurrent request?) and credit cost per STT/TTS call.
3. Exact video length/format and whether a code repo link is wanted.
