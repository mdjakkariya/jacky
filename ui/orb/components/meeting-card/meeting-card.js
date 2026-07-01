/** Meeting recording + minutes card (factory-function module, no custom element,
 *  no shadow DOM). Listens for daemon "meeting" events and renders the correct
 *  phase card into the chat log. */
import { daemon } from "../../lib/daemon.js";
import { copyText } from "../../lib/clipboard.js";

// ── SVG snippets (inline; no external assets) ──────────────────────────────
const SVG_WARN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>';
const SVG_FILE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M5 3h9l5 5v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/></svg>';
const SVG_FOLDER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const SVG_COPY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
const SVG_LINES = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg>';
const SVG_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>';

const fmt = (s) =>
  String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");

// ── parseMinutes ────────────────────────────────────────────────────────────

/** Parse the fixed meeting-minutes markdown format produced by the daemon.
 * Returns {title, date, duration, attendees, summary, decisions, actions, openQuestions}.
 * PURE — no side effects, suitable for unit testing without DOM.
 * @param {string} md
 * @returns {{title:string, date:string, duration:string, attendees:string,
 *            summary:string, decisions:string[], actions:{owner:string,task:string}[],
 *            openQuestions:string[]}}
 */
export function parseMinutes(md) {
  const lines = (md || "").split("\n");
  let title = "", date = "", duration = "", attendees = "", summary = "";
  const decisions = [], actions = [], openQuestions = [];

  let section = null;
  let summaryLines = [];

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.trim();

    // Title: first H1
    if (line.startsWith("# ") && !title) {
      title = line.slice(2).trim();
      continue;
    }

    // Meta bullets: - **Date:** / **Duration:** / **Attendees:** (colon inside the **)
    const metaM = line.match(/^-\s+\*\*([^*:]+):\*\*\s*(.*)/);
    if (metaM) {
      const key = metaM[1].toLowerCase();
      const val = metaM[2].trim();
      if (key === "date") date = val;
      else if (key === "duration") duration = val;
      else if (key === "attendees") attendees = val;
      continue;
    }

    // Section headers H2
    if (line.startsWith("## ")) {
      const hdr = line.slice(3).trim().toLowerCase();
      if (hdr === "summary") { section = "summary"; summaryLines = []; continue; }
      else if (hdr === "decisions") { section = "decisions"; continue; }
      else if (hdr === "action items") { section = "actions"; continue; }
      else if (hdr === "open questions") { section = "openQuestions"; continue; }
      else { section = null; continue; }
    }

    if (section === "summary") {
      // Collect non-empty lines
      if (line) summaryLines.push(line);
      else if (summaryLines.length) { summary = summaryLines.join(" "); section = null; }
      continue;
    }

    if (section === "decisions") {
      if (line.startsWith("- ")) {
        const item = line.slice(2).trim();
        if (item.toLowerCase() !== "none") decisions.push(item);
      }
      continue;
    }

    if (section === "actions") {
      if (line.startsWith("- ")) {
        const item = line.slice(2).trim();
        if (item.toLowerCase() !== "none") {
          // em dash split: "owner — task"
          const emM = item.match(/^(.+?)\s+—\s+(.+)$/);
          if (emM) {
            actions.push({ owner: emM[1].trim(), task: emM[2].trim() });
          } else {
            actions.push({ owner: "", task: item });
          }
        }
      }
      continue;
    }

    if (section === "openQuestions") {
      if (line.startsWith("- ")) {
        const item = line.slice(2).trim();
        if (item.toLowerCase() !== "none") openQuestions.push(item);
      }
      continue;
    }
  }

  // flush summary if file ends without blank line
  if (section === "summary" && summaryLines.length && !summary) {
    summary = summaryLines.join(" ");
  }

  return { title, date, duration, attendees, summary, decisions, actions, openQuestions };
}

// ── Recording card ──────────────────────────────────────────────────────────

function getOrCreateRecCard(log, m) {
  let card = log.querySelector("#meeting-card");
  if (!card) {
    card = document.createElement("div");
    card.id = "meeting-card";
    card.className = "rec-card";
    // Build the waveform bars
    const bars = Array.from({ length: 28 }, () => {
      const dur = (0.7 + Math.random() * 0.7).toFixed(2);
      const delay = (-Math.random() * 1).toFixed(2);
      return `<i style="animation-duration:${dur}s;animation-delay:${delay}s"></i>`;
    }).join("");

    card.innerHTML =
      '<div class="rec-top">' +
        '<span class="dot"></span>' +
        '<span class="ttl"></span>' +
        '<span class="pill">PAUSED</span>' +
        '<span class="timer">00:00</span>' +
      '</div>' +
      '<div class="rec-body">' +
        '<div class="wave">' + bars + '</div>' +
        '<div class="caplegend">' +
          '<span><span class="who-dot you"></span>You</span>' +
          '<span class="them-label"><span class="who-dot them"></span>Participants</span>' +
          '<span class="sep">·</span><span>English · on-device</span>' +
        '</div>' +
        '<div class="warnline">' + SVG_WARN +
          '<span>Capturing your side only — the other participants’ audio isn’t being recorded. You can grant Audio Capture in Settings and re-record.</span>' +
        '</div>' +
      '</div>' +
      '<div class="rec-foot">' +
        '<span class="note"><span class="ld"></span><span class="note-txt">Audio saved locally · transcribed when you stop</span></span>' +
        '<div class="rec-actions">' +
          '<button class="btn pause-btn">⏸ Pause</button>' +
          '<button class="btn danger stop-btn">■ Stop &amp; summarize</button>' +
        '</div>' +
      '</div>';

    card.querySelector(".pause-btn").addEventListener("click", () => {
      daemon.meetingPause();
    });
    card.querySelector(".stop-btn").addEventListener("click", () => {
      daemon.meetingStop();
    });

    log.appendChild(card);
    if (log.scroll) log.scroll();
  }
  return card;
}

function updateRecCard(card, m) {
  const title = m.title || "Meeting";
  card.querySelector(".ttl").textContent = "Recording — " + title;

  if (m.mic_only) card.classList.add("miconly");
  else card.classList.remove("miconly");

  if (m.paused) {
    card.classList.add("paused");
    card.querySelector(".pause-btn").textContent = "▶ Resume";
    card.querySelector(".note-txt").textContent = "Paused — audio is not being captured";
  } else {
    card.classList.remove("paused");
    card.querySelector(".pause-btn").textContent = "⏸ Pause";
    card.querySelector(".note-txt").textContent = "Audio saved locally · transcribed when you stop";
  }

  // Rewire buttons on state change (pause → resume swap)
  const pauseBtn = card.querySelector(".pause-btn");
  // Clone + replace to drop the old listener, then rewire
  const newPauseBtn = pauseBtn.cloneNode(true);
  pauseBtn.replaceWith(newPauseBtn);
  if (m.paused) {
    newPauseBtn.addEventListener("click", () => { daemon.meetingResume(); });
  } else {
    newPauseBtn.addEventListener("click", () => { daemon.meetingPause(); });
  }
}

function startTimer(card, seedSeconds) {
  // Stop any existing timer on this card
  stopTimer(card);

  let elapsed = Math.floor(seedSeconds || 0);
  const timerEl = card.querySelector(".timer");
  if (timerEl) timerEl.textContent = fmt(elapsed);

  const id = setInterval(() => {
    // Check paused state via class (updated by updateRecCard)
    if (!card.classList.contains("paused")) {
      elapsed++;
      if (timerEl) timerEl.textContent = fmt(elapsed);
    }
  }, 1000);
  card._mtgTimerId = id;
}

function stopTimer(card) {
  if (card._mtgTimerId != null) {
    clearInterval(card._mtgTimerId);
    card._mtgTimerId = null;
  }
}

// ── Processing steps card ───────────────────────────────────────────────────

function showProcessingCard(log) {
  // Remove rec-card if present
  const existing = log.querySelector("#meeting-card");
  if (existing) {
    stopTimer(existing);
    existing.remove();
  }

  let proc = log.querySelector("#meeting-processing");
  if (proc) return proc; // already shown

  proc = document.createElement("div");
  proc.id = "meeting-processing";
  proc.className = "confirm"; // reuse card base style

  const head = document.createElement("div"); head.className = "h";
  head.innerHTML = SVG_FILE + " Processing meeting";

  const ul = document.createElement("ul");
  ul.className = "mtg-steps";

  const stepDefs = [
    { label: "Finalizing the recording", meta: "" },
    { label: "Transcribing · English", meta: "on-device" },
    { label: "Writing minutes (summarize → reduce)", meta: "qwen3:8b" },
  ];

  stepDefs.forEach((s) => {
    const li = document.createElement("li");
    const ic = document.createElement("span"); ic.className = "ic";
    const txt = document.createTextNode(" " + s.label);
    li.appendChild(ic);
    li.appendChild(txt);
    if (s.meta) {
      const meta = document.createElement("span"); meta.className = "meta";
      meta.textContent = s.meta;
      li.appendChild(meta);
    }
    ul.appendChild(li);
  });

  proc.appendChild(head);
  proc.appendChild(ul);
  log.appendChild(proc);
  if (log.scroll) log.scroll();
  return proc;
}

function advanceProcessingSteps(proc, state) {
  const lis = proc.querySelectorAll("li");
  // transcribing → step 0 done, step 1 active; summarizing → step 1 done, step 2 active
  let doneCount = 0;
  let activeIdx = -1;
  if (state === "transcribing") { doneCount = 1; activeIdx = 1; }
  else if (state === "summarizing") { doneCount = 2; activeIdx = 2; }

  lis.forEach((li, i) => {
    li.classList.remove("done", "active");
    const ic = li.querySelector(".ic");
    if (i < doneCount) {
      li.classList.add("done");
      if (ic) ic.innerHTML = SVG_CHECK;
    } else if (i === activeIdx) {
      li.classList.add("active");
      if (ic) ic.innerHTML = "";
    }
  });
}

// ── Minutes card ────────────────────────────────────────────────────────────

/** Render the completed minutes card from a /meeting/last response.
 * @param {HTMLElement} log
 * @param {{ok:boolean, id:string, dir:string, mic_only:boolean, minutes_md:string}} r
 */
export function renderMinutes(log, r) {
  // Remove the processing card if still present
  const proc = log.querySelector("#meeting-processing");
  if (proc) proc.remove();

  const parsed = parseMinutes(r.minutes_md || "");
  const { title, date, duration, attendees, summary, decisions, actions, openQuestions } = parsed;

  const card = document.createElement("div");
  card.id = "meeting-minutes";
  card.className = "min";

  // ── header ──
  const minH = document.createElement("div"); minH.className = "min-h";

  const loc = document.createElement("span"); loc.className = "loc";
  loc.innerHTML = SVG_FOLDER;
  const crumb = document.createElement("span"); crumb.className = "crumb";
  crumb.textContent = "Meetings";
  const _sep = document.createElement("i");
  _sep.textContent = "›";
  crumb.appendChild(_sep);
  crumb.appendChild(document.createTextNode(title || "Meeting"));
  const ext = document.createElement("span"); ext.className = "ext";
  ext.textContent = "minutes.md";
  loc.appendChild(crumb);
  loc.appendChild(ext);

  const h3 = document.createElement("h3");
  h3.textContent = (title || "Meeting") + " — Minutes";

  const meta = document.createElement("div"); meta.className = "meta";
  if (date) { const s = document.createElement("span"); s.textContent = date; meta.appendChild(s); }
  if (duration) { const s = document.createElement("span"); s.textContent = "Duration " + duration; meta.appendChild(s); }
  if (attendees) { const s = document.createElement("span"); s.textContent = attendees; meta.appendChild(s); }

  minH.appendChild(loc);
  minH.appendChild(h3);
  minH.appendChild(meta);

  // ── preview body ──
  const minPrev = document.createElement("div"); minPrev.className = "min-prev";

  const st = document.createElement("div"); st.className = "st";
  st.innerHTML = SVG_LINES + " Summary";

  const sumP = document.createElement("p"); sumP.className = "sum";
  sumP.textContent = summary || "";

  const statsDiv = document.createElement("div"); statsDiv.className = "mtg-stats";
  [
    { n: decisions.length, label: "decision" },
    { n: actions.length, label: "action item" },
    { n: openQuestions.length, label: "open question" },
  ].forEach(({ n, label }) => {
    const chip = document.createElement("span"); chip.className = "stat";
    chip.innerHTML = "<b>" + n + "</b> " + (n === 1 ? label : label + "s");
    statsDiv.appendChild(chip);
  });

  // Action items preview (first 2)
  const aiList = document.createElement("ul"); aiList.className = "aiprev";
  const preview = actions.slice(0, 2);
  preview.forEach(({ owner, task }) => {
    const li = document.createElement("li"); li.className = "ai";
    const box = document.createElement("span"); box.className = "box";
    const txt = document.createElement("span");
    txt.textContent = task;
    if (owner) {
      const who = document.createElement("span"); who.className = "who";
      who.textContent = owner;
      txt.appendChild(who);
    }
    li.appendChild(box);
    li.appendChild(txt);
    aiList.appendChild(li);
  });
  const remaining = actions.length - preview.length;
  if (remaining > 0) {
    const more = document.createElement("li"); more.className = "more-ai";
    more.textContent = "+" + remaining + " more action item" + (remaining > 1 ? "s" : "");
    aiList.appendChild(more);
  }

  minPrev.appendChild(st);
  minPrev.appendChild(sumP);
  minPrev.appendChild(statsDiv);
  minPrev.appendChild(aiList);

  // ── footer ──
  const foot = document.createElement("div"); foot.className = "min-foot";

  const openBtn = document.createElement("button"); openBtn.className = "fbtn primary";
  openBtn.innerHTML = SVG_FILE + " Open minutes";

  const revealBtn = document.createElement("button"); revealBtn.className = "fbtn";
  revealBtn.innerHTML = SVG_FOLDER + " Reveal folder";

  const copyBtn = document.createElement("button"); copyBtn.className = "fbtn";
  copyBtn.innerHTML = SVG_COPY + " Copy";

  // Open minutes / Reveal folder: copy the path to clipboard (no daemon route exists)
  const dir = r.dir || "";
  openBtn.title = dir;
  openBtn.addEventListener("click", () => {
    copyText(dir);
    openBtn.textContent = "Path copied";
    setTimeout(() => { openBtn.innerHTML = SVG_FILE + " Open minutes"; }, 1500);
  });

  revealBtn.title = dir;
  revealBtn.addEventListener("click", () => {
    copyText(dir);
    revealBtn.textContent = "Path copied";
    setTimeout(() => { revealBtn.innerHTML = SVG_FOLDER + " Reveal folder"; }, 1500);
  });

  copyBtn.addEventListener("click", () => {
    copyText(r.minutes_md || "");
    copyBtn.textContent = "Copied!";
    setTimeout(() => { copyBtn.innerHTML = SVG_COPY + " Copy"; }, 1500);
  });

  foot.appendChild(openBtn);
  foot.appendChild(revealBtn);
  foot.appendChild(copyBtn);

  card.appendChild(minH);
  card.appendChild(minPrev);
  card.appendChild(foot);

  log.appendChild(card);
  if (log.scroll) log.scroll();
  return card;
}

// ── Main entry ──────────────────────────────────────────────────────────────

/** Remove any active meeting card and minutes card from the log.
 * @param {HTMLElement} log
 */
export function clearMeeting(log) {
  const rec = log.querySelector("#meeting-card");
  if (rec) { stopTimer(rec); rec.remove(); }
  const proc = log.querySelector("#meeting-processing");
  if (proc) proc.remove();
  const mins = log.querySelector("#meeting-minutes");
  if (mins) mins.remove();
}

/** Single entry point — called from daemon.on("meeting", m => renderMeeting(log, m)).
 * m = {type:"meeting", state, elapsed_s, recorded_s, mic_only, paused, title}
 * state ∈ {idle, recording, paused, transcribing, summarizing, done}
 * @param {HTMLElement} log
 * @param {{state:string, elapsed_s:number, mic_only:boolean, paused:boolean, title:string}} m
 */
export function renderMeeting(log, m) {
  const { state } = m;

  if (state === "recording" || state === "paused") {
    const card = getOrCreateRecCard(log, m);
    updateRecCard(card, m);
    // Re-seed the timer from server elapsed_s on each event
    startTimer(card, Math.floor(m.elapsed_s || 0));
    return;
  }

  if (state === "transcribing" || state === "summarizing") {
    // Stop recording timer if still running
    const rec = log.querySelector("#meeting-card");
    if (rec) { stopTimer(rec); rec.remove(); }

    const proc = showProcessingCard(log);
    advanceProcessingSteps(proc, state);
    return;
  }

  if (state === "done") {
    // Clear recording + processing cards
    const rec = log.querySelector("#meeting-card");
    if (rec) { stopTimer(rec); rec.remove(); }
    const proc = log.querySelector("#meeting-processing");
    if (proc) proc.remove();

    daemon.meetingLast().then((r) => {
      if (r && r.ok) renderMinutes(log, r);
    }).catch(() => {});
    return;
  }

  if (state === "idle") {
    // Idle: nothing to render; clear any lingering card
    clearMeeting(log);
  }
}
