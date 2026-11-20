/* Saarthi hands-free voice mode (same flow as the Gnani build, on Google/browser speech):
   talk-first greeting -> listen -> agent acts (Gemini tools / playbooks) -> speaks -> listens again.
   Agent alerts are spoken too. Say "bas" / "stop" or press Esc to end. Uses the browser's Web Speech API
   (Chrome/Edge: Google speech recognition, hi-IN for Hindi + Hinglish + English). */
(function () {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const css = `#vorb{position:fixed;right:22px;bottom:92px;z-index:1500;display:none;align-items:center;gap:10px;max-width:420px;
    background:rgba(4,8,18,.94);border:1px solid #26365a;border-radius:18px;padding:10px 14px 10px 10px;box-shadow:0 10px 40px rgba(0,0,0,.5);font:500 12.5px Inter,system-ui}
  #vorb .o{width:44px;height:44px;border-radius:50%;flex:none;background:radial-gradient(circle at 35% 35%,#5eead4,#0f766e);box-shadow:0 0 22px rgba(45,212,191,.55)}
  #vorb.listening .o{animation:vp 1.1s infinite}#vorb.thinking .o{background:radial-gradient(circle at 35% 35%,#fde68a,#b45309);animation:vs 1s linear infinite}
  #vorb.speaking .o{background:radial-gradient(circle at 35% 35%,#c4b5fd,#6d28d9);animation:vp .6s infinite}
  @keyframes vp{0%{box-shadow:0 0 0 0 rgba(45,212,191,.6)}100%{box-shadow:0 0 0 16px rgba(45,212,191,0)}}
  @keyframes vs{to{filter:hue-rotate(40deg)}}
  #vorb .t{color:#e6edf7;line-height:1.35}#vorb .t small{display:block;color:#8a9bb8;font-size:10.5px;letter-spacing:1px;font-weight:700}
  #vorb button{margin-left:auto;background:#1b2a46;border:0;color:#e6edf7;border-radius:8px;padding:5px 9px;cursor:pointer;font:600 11px Inter}`;
  const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
  const orb = document.createElement("div"); orb.id = "vorb";
  orb.innerHTML = `<div class="o"></div><div class="t"><small id="vst">VOICE MODE</small><span id="vtx"></span></div><button id="vstop">Stop · Esc</button>`;
  document.body.appendChild(orb);
  const $s = (id) => document.getElementById(id);

  const V = { on: false, rec: null, speaking: false, queue: [], opts: {} };
  function state(s, text) {
    orb.className = s; $s("vst").textContent = { listening: "LISTENING · boliye", thinking: "AGENTS WORKING", speaking: "SAARTHI", idle: "VOICE MODE" }[s] || s;
    if (text !== undefined) $s("vtx").textContent = text;
  }
  function voiceFor(text) {
    const vs = speechSynthesis.getVoices();
    const hindi = /[ऀ-ॿ]/.test(text);
    return (hindi && vs.find((v) => /hi[-_]IN/i.test(v.lang))) || vs.find((v) => /en[-_]IN/i.test(v.lang)) ||
      vs.find((v) => /hi[-_]IN/i.test(v.lang)) || null;
  }
  function speak(text) {
    return new Promise((res) => {
      if (!window.speechSynthesis || !text) return res();
      V.speaking = true; state("speaking", text);
      const u = new SpeechSynthesisUtterance(text.replace(/[*_#•]/g, "").slice(0, 600));
      const v = voiceFor(text); if (v) { u.voice = v; u.lang = v.lang } else u.lang = "en-IN";
      u.rate = 1.04;
      const done = () => { V.speaking = false; res() };
      u.onend = done; u.onerror = done;
      speechSynthesis.cancel(); speechSynthesis.speak(u);
      setTimeout(done, 25000);
    });
  }
  async function flush() { while (V.queue.length) await speak(V.queue.shift()) }
  function listen() {
    if (!V.on) return;
    if (!SR) { state("idle", "Voice input needs Chrome or Edge."); return }
    const rec = new SR(); V.rec = rec; rec.lang = V.opts.lang || "hi-IN"; rec.interimResults = true; rec.continuous = false;
    let finalText = "", got = false;
    state("listening", "Boliye… (Hindi, Hinglish ya English)");
    rec.onresult = (e) => {
      got = true; const t = [...e.results].map((r) => r[0].transcript).join("");
      $s("vtx").textContent = "“" + t + "”"; if (V.opts.input) V.opts.input.value = t;
      if (e.results[e.results.length - 1].isFinal) finalText = t;
    };
    rec.onerror = () => {};
    rec.onend = async () => {
      V.rec = null; if (!V.on) return;
      if (!finalText) { await flush(); return setTimeout(listen, got ? 150 : 350) }
      if (/^\s*(bas|stop|ruko|band karo|bye|thank you|shukriya|dhanyavaad)\b/i.test(finalText)) { await speak("Theek hai. Main yahin hoon, zarurat ho to bulaiye."); return stop() }
      state("thinking", "“" + finalText + "”");
      let reply = "";
      try { reply = await V.opts.ask(finalText) } catch (e) { reply = "Network slow hai, ek baar phir boliye." }
      if (V.opts.input) V.opts.input.value = "";
      await speak(reply); await flush(); listen();
    };
    try { rec.start() } catch (e) { setTimeout(listen, 500) }
  }
  async function start() {
    if (V.on) return; V.on = true; orb.style.display = "flex"; window.speechSynthesis && speechSynthesis.getVoices();
    V.opts.onState && V.opts.onState(true);
    let hello = "";
    try { hello = V.opts.greet ? await V.opts.greet() : "" } catch (e) {}
    if (hello) await speak(hello);
    listen();
  }
  function stop() {
    V.on = false; V.queue = []; try { V.rec && V.rec.abort() } catch (e) {}
    window.speechSynthesis && speechSynthesis.cancel(); orb.style.display = "none"; state("idle", "");
    V.opts.onState && V.opts.onState(false);
  }
  /** Speak an agent alert. Interrupts listening only between sentences, never mid-answer. */
  function announce(text) {
    if (!V.on || !text) return;
    if (V.speaking) { V.queue.push(text); return }
    if (V.rec) { V.queue.push(text); try { V.rec.abort() } catch (e) {} return }
    V.queue.push(text);
  }
  $s("vstop").onclick = stop;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && V.on) stop() });
  window.SaarthiVoice = { init: (o) => { V.opts = o }, start, stop, announce, speak, get on() { return V.on } };
})();
