/** About panel: shows the app version and offers a manual update check (the only
 *  time this window goes online). Thin client over lib/{dom,tauri,format}. */
import { $ } from "../lib/dom.js";
import { invoke } from "../lib/tauri.js";
import { cmpVer } from "../lib/format.js";

// Release source (matches the git remote: github.com/mdjakkariya/jacky).
const REPO = "mdjakkariya/jacky";
const RELEASES_API = "https://api.github.com/repos/" + REPO + "/releases/latest";
const RELEASES_PAGE = "https://github.com/" + REPO + "/releases/latest";

let current = "0.0.0";

function setStatus(text, cls) {
  const s = $("status");
  s.className = cls || "";
  s.textContent = text || "";
}

async function loadVersion() {
  const v = await invoke("app_version");   // undefined in a plain browser
  if (v != null) current = v;
  $("version").textContent = current;
}

async function checkUpdates() {
  const btn = $("check");
  btn.disabled = true;
  $("update-link").style.display = "none";
  $("status").innerHTML = '<span class="spinner"></span>Checking…';
  $("status").className = "";
  try {
    const resp = await fetch(RELEASES_API, { headers: { "Accept": "application/vnd.github+json" } });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    const latest = (data.tag_name || data.name || "").trim();
    if (!latest) { setStatus("No releases published yet.", "err"); return; }

    const diff = cmpVer(latest, current);
    if (diff > 0) {
      setStatus("Update available: " + latest.replace(/^v/i, ""), "warn");
      const link = $("update-link");
      link.style.display = "inline-block";
      link.onclick = () => invoke("open_external", { url: RELEASES_PAGE });
    } else {
      setStatus("You're up to date.", "ok");
    }
  } catch (e) {
    setStatus("Couldn't check for updates. Check your connection.", "err");
  } finally {
    btn.disabled = false;
  }
}

$("check").addEventListener("click", checkUpdates);
loadVersion();
