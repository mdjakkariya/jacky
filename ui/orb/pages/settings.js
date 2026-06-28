/** Settings window orchestration. Owns the model/provider/STT/secrets form glue
 *  (inherently tied to load()/save() and spanning tabs) and wires the self-contained
 *  components: settings-tabs, model selects, voice-download, permissions-list,
 *  access-list, report-sheet. Moved from settings.html; fetch/clipboard/tauri now via lib. */
import { daemon } from "../lib/daemon.js";
import { $ } from "../lib/dom.js";
import "../components/settings-tabs/settings-tabs.js";
import "../components/permissions-list/permissions-list.js";
import "../components/access-list/access-list.js";
import "../components/voice-download/voice-download.js";
import { setupReportSheet } from "../components/report-sheet/report-sheet.js";

const CHECKS = ["tts_enabled", "barge_in", "aec", "allow_app_control", "allow_system_info", "allow_memory", "allow_file_search", "allow_clipboard", "allow_reminders", "allow_file_io", "allow_web"];

// --- status line ------------------------------------------------------------
let _statusTimer = null;
function setStatus(text, isError) {
  const el = $("status");
  el.style.color = isError ? "#ff6b6b" : "";
  el.textContent = text;
  if (_statusTimer) clearTimeout(_statusTimer);
  if (text) { _statusTimer = setTimeout(() => { el.textContent = ""; }, 3000); }
}

// --- tabs: lazy-load perms + voice status on switch -------------------------
const tabs = document.querySelector("settings-tabs");
tabs.addEventListener("tab-change", (e) => {
  if (e.detail === "perms") $("permsList").load();
  if (e.detail === "listen") $("voiceSetupCard").loadStatus();
});

// --- access-list status events -> page status line --------------------------
const access = document.querySelector("access-list");
if (access) access.addEventListener("status", (e) => setStatus(e.detail.msg, e.detail.isError));

// --- model pickers (provider + Claude/Ollama/STT, with a Custom… escape hatch) ---
const CLAUDE_SUGGESTIONS = ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8"];
const STT_SUGGESTIONS = ["tiny.en", "base.en", "small.en", "medium.en", "distil-large-v3", "large-v3"];

function setProviderUI(p) {
  $("cloud-box").classList.toggle("hidden", p !== "anthropic");
  $("cloud-note-box").classList.toggle("hidden", p !== "anthropic");
  $("local-box").classList.toggle("hidden", p === "anthropic");
  $("providerDesc").textContent = p === "anthropic"
    ? "Faster, but your requests are sent to Anthropic."
    : "Runs privately on your Mac.";
}
$("provider").addEventListener("change", () => setProviderUI($("provider").value));

function populateClaudeModels(current) {
  const sel = $("anthropicModel"); sel.innerHTML = "";
  CLAUDE_SUGGESTIONS.forEach((m) => { const o = document.createElement("option"); o.value = m; o.textContent = m; sel.appendChild(o); });
  const custom = document.createElement("option"); custom.value = "__custom__"; custom.textContent = "Custom…"; sel.appendChild(custom);
  if (current && CLAUDE_SUGGESTIONS.indexOf(current) === -1) {
    sel.value = "__custom__"; $("anthropicModelCustom").value = current; $("anthropicModelCustom").classList.remove("hidden");
  } else {
    sel.value = current || CLAUDE_SUGGESTIONS[0]; $("anthropicModelCustom").classList.add("hidden");
  }
}
$("anthropicModel").addEventListener("change", () => {
  $("anthropicModelCustom").classList.toggle("hidden", $("anthropicModel").value !== "__custom__");
});
function selectedClaudeModel() {
  const v = $("anthropicModel").value;
  return v === "__custom__" ? $("anthropicModelCustom").value.trim() : v;
}

function populateSttModels(current) {
  const sel = $("sttModel"); sel.innerHTML = "";
  STT_SUGGESTIONS.forEach((m) => { const o = document.createElement("option"); o.value = m; o.textContent = m; sel.appendChild(o); });
  const custom = document.createElement("option"); custom.value = "__custom__"; custom.textContent = "Custom…"; sel.appendChild(custom);
  if (current && STT_SUGGESTIONS.indexOf(current) === -1) {
    sel.value = "__custom__"; $("sttModelCustom").value = current; $("sttModelCustom").classList.remove("hidden");
  } else {
    sel.value = current || "small.en"; $("sttModelCustom").classList.add("hidden");
  }
}
$("sttModel").addEventListener("change", () => {
  $("sttModelCustom").classList.toggle("hidden", $("sttModel").value !== "__custom__");
});
function selectedSttModel() {
  const v = $("sttModel").value;
  return v === "__custom__" ? $("sttModelCustom").value.trim() : v;
}

async function populateModels(current) {
  const sel = $("ollamaModel"); sel.innerHTML = "";
  let models = [];
  try { models = ((await daemon.models()).models) || []; } catch (e) { /* leave empty */ }
  if (!models.length) {
    $("ollamaNote").textContent = "No Ollama models found (is `ollama serve` running?).";
    if (current) { models = [current]; }
  } else { $("ollamaNote").textContent = ""; }
  if (current && models.indexOf(current) === -1) models.unshift(current);
  models.forEach((name) => {
    const o = document.createElement("option"); o.value = name; o.textContent = name;
    if (name === current) o.selected = true;
    sel.appendChild(o);
  });
}

// --- web search config visibility -------------------------------------------
function updateWebUI() {
  const on = $("allow_web").checked;
  $("web-config").classList.toggle("hidden", !on);
  $("web-key-stack").classList.toggle("hidden", $("web_provider").value === "ddgs");
}
$("allow_web").addEventListener("change", updateWebUI);
$("web_provider").addEventListener("change", updateWebUI);

function setEnabled(on) { ["save"].forEach((id) => { $(id).disabled = !on; }); }

// --- load / save ------------------------------------------------------------
async function load() {
  $("status").textContent = ""; $("banner").classList.remove("show");
  let s = null;
  for (let i = 0; i < 12; i++) {
    try { s = await daemon.settings(); break; } catch (e) {}
    setEnabled(false);
    await new Promise((res) => { setTimeout(res, 800); });
  }
  if (s === null) { $("banner").classList.add("show"); setEnabled(false); return; }
  setEnabled(true);
  const provider = s.llm_provider || "ollama";
  $("provider").value = provider;
  setProviderUI(provider);
  populateClaudeModels(s.anthropic_model || "");
  $("follow_up_window_s").value = s.follow_up_window_s != null ? s.follow_up_window_s : 30;
  $("end_silence_ms").value = s.end_silence_ms != null ? s.end_silence_ms : 1400;
  $("max_utterance_s").value = s.max_utterance_s != null ? s.max_utterance_s : 30;
  $("vad_threshold").value = s.vad_threshold != null ? s.vad_threshold : 0.5;
  $("stt_engine").value = s.stt_engine || "faster_whisper";
  populateSttModels(s.stt_model || "");
  CHECKS.forEach((k) => { $(k).checked = !!s[k]; });
  $("web_provider").value = s.web_provider || "auto";
  updateWebUI();
  const sec = s._secrets || {};
  $("keyState").textContent = sec.anthropic_api_key ? "— saved (leave blank to keep)" : "— not set";
  $("webKeyState").textContent = sec.web_api_key ? "— saved (leave blank to keep)" : "— not set";
  $("voiceSetupCard").loadStatus();
  await populateModels(s.llm_model || "");
}

async function saveSecret(name, input, stateEl) {
  const v = input.value.trim();
  if (!v) return; // blank = keep existing
  await daemon.secret(name, v);
  input.value = ""; stateEl.textContent = "— saved (leave blank to keep)";
}

async function save() {
  const provider = $("provider").value;
  const body = {
    llm_provider: provider,
    llm_model: $("ollamaModel").value,
    anthropic_model: selectedClaudeModel(),
    web_provider: $("web_provider").value,
    follow_up_window_s: Number($("follow_up_window_s").value),
    end_silence_ms: Number($("end_silence_ms").value),
    max_utterance_s: Number($("max_utterance_s").value),
    vad_threshold: Number($("vad_threshold").value),
    stt_engine: $("stt_engine").value,
    stt_model: selectedSttModel(),
  };
  CHECKS.forEach((k) => { body[k] = $(k).checked; });
  try {
    await daemon.setSettings(body);
    await saveSecret("anthropic_api_key", $("anthropicKey"), $("keyState"));
    await saveSecret("web_api_key", $("webKey"), $("webKeyState"));
    setStatus("Saved.");
  } catch (e) {
    setStatus("Save failed — is Jack running?", true);
  }
}

$("save").addEventListener("click", save);
$("reload").addEventListener("click", load);
$("retry").addEventListener("click", load);

// --- report sheet + tray deep-links -----------------------------------------
const report = setupReportSheet(setStatus);
window.__openReport = report.open;
if (location.hash.indexOf("report") >= 0) { report.open(); }
window.__openVoice = function () { tabs.select("listen"); };
if (location.hash.indexOf("voice") >= 0) { window.__openVoice(); }

load();
