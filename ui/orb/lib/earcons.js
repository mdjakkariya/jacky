/** WebAudio earcons, on-device, no asset. Factory so each surface (orb/chat) sets
 *  its own master gain. Disabled via localStorage "jackEarcons" = "0". */
export function createEarcons({ gain = 0.16 } = {}) {
  let ac = null, acGain = null;
  let lastEar = { s: "", t: 0 };

  function enabled() { try { return localStorage.getItem("jackEarcons") !== "0"; } catch (e) { return true; } }

  function audioCtx() {
    if (ac) return ac;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext; if (!Ctx) return null;
      ac = new Ctx(); acGain = ac.createGain(); acGain.gain.value = gain; acGain.connect(ac.destination);
    } catch (e) { ac = null; }
    return ac;
  }

  function blip(freq, start, dur, peak, type) {
    const c = audioCtx(); if (!c) return;
    const o = c.createOscillator(), g = c.createGain();
    o.type = type || "sine"; o.frequency.value = freq; o.connect(g); g.connect(acGain);
    const t0 = c.currentTime + start;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(peak, t0 + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.start(t0); o.stop(t0 + dur + 0.02);
  }

  // Ascending pair = voice on; descending = back to chat.
  function playMode(mode) {
    if (!enabled()) return;
    const c = audioCtx(); if (!c) return;
    const go = () => {
      if (mode === "voice") { blip(587.33, 0, 0.14, 0.85, "sine"); blip(880, 0.10, 0.20, 0.8, "sine"); }
      else { blip(880, 0, 0.12, 0.7, "sine"); blip(587.33, 0.10, 0.16, 0.7, "sine"); }
    };
    if (c.state === "suspended") { c.resume().then(go).catch(go); } else { go(); }
  }

  const STATE_CUES = {
    listening: () => { blip(659.25, 0, 0.14, 0.9, "sine"); blip(987.77, 0.09, 0.18, 0.8, "sine"); },
    thinking: () => { blip(440, 0, 0.07, 0.95, "triangle"); blip(440, 0.18, 0.08, 0.9, "triangle"); },
  };
  function playState(next) {
    if (!enabled()) return;
    const fn = STATE_CUES[next]; if (!fn) return; // idle/talking: no cue
    const now = Date.now();
    if (lastEar.s === next && (now - lastEar.t) < 1500) return; // debounce VAD flutter
    const c = audioCtx(); if (!c) return;
    if (c.state === "suspended") { try { c.resume(); } catch (e) {} }
    lastEar = { s: next, t: now }; fn();
  }

  function resumeOnGesture() {
    ["pointerdown", "keydown"].forEach((ev) =>
      window.addEventListener(ev, () => { const c = audioCtx(); if (c && c.state === "suspended") { try { c.resume(); } catch (e) {} } }, { once: true, passive: true }));
  }

  return { enabled, blip, playMode, playState, resumeOnGesture };
}
