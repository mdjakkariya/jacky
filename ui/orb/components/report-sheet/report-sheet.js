/** Debug-report bottom sheet + GitHub-issue flow. NOTE: implemented as a controller
 *  function rather than a custom element because its DOM is split across the pane,
 *  the backdrop, and footer buttons (no single wrapping element). Wires those by id
 *  and returns { open } for the tray "#report" deep-link. Moved from settings.html. */
import { daemon } from "../../lib/daemon.js";
import { $ } from "../../lib/dom.js";
import { copyText } from "../../lib/clipboard.js";
import { openExternal, revealInFinder, hasTauri } from "../../lib/tauri.js";

const ISSUE_URL = "https://github.com/mdjakkariya/autobot/issues/new";
const REDACT_HINT = "Redacted — no audio, no API keys.";

export function setupReportSheet(setStatus) {
  let hintTimer = null;

  async function buildReport() {
    let txt = "";
    try { txt = (await daemon.report()).report || ""; }
    catch (e) { setStatus("Couldn't build report — is Jack running?", true); return ""; }
    $("reportOut").value = txt;
    return txt;
  }
  async function copyReport() {
    const txt = $("reportOut").value || await buildReport();
    if (!txt) return;
    const ok = await copyText(txt);
    const tip = $("copyTip");
    tip.textContent = ok ? "Copied" : "Press ⌘C";
    tip.classList.add("show");
    setTimeout(() => tip.classList.remove("show"), 1400);
    if (!ok) { $("reportOut").focus(); $("reportOut").select(); }
  }
  function openReport() {
    $("reportPane").classList.add("open");
    $("reportBackdrop").classList.add("open");
    $("raiseIssue").classList.add("hidden");
    $("reportActions").classList.remove("hidden");
  }
  function closeReport() {
    $("reportPane").classList.remove("open");
    $("reportBackdrop").classList.remove("open");
    $("reportActions").classList.add("hidden");
    $("raiseIssue").classList.remove("hidden");
  }
  async function raiseIssue() {
    const txt = await buildReport();
    if (!txt) return;
    await copyText(txt); // so the panel's "Copied to your clipboard" is true
    openReport();
  }
  function flashHint(msg) {
    const el = $("reportHint");
    el.textContent = msg;
    if (hintTimer) clearTimeout(hintTimer);
    hintTimer = setTimeout(() => { el.textContent = REDACT_HINT; }, 3000);
  }
  async function revealReport(e) {
    if (e) e.preventDefault();
    try {
      const r = await daemon.reportFile();
      if (r && r.path) { await revealInFinder(r.path); flashHint("Saved — revealed in Finder."); }
    } catch (e2) { flashHint("Couldn't save the file."); }
  }
  async function reportIssue() {
    const txt = $("reportOut").value || await buildReport();
    if (!txt) return;
    // The report is several KB but a new-issue URL caps at ~8 KB, so carry it on the
    // clipboard and leave the form's Debug-report field empty (paste once, cleanly).
    await copyText(txt);
    const appLine = txt.split("\n").find((l) => l.indexOf("**App**") === 0) || "";
    const m = /v([0-9][^ \n·]*)/.exec(appLine);
    const url = ISSUE_URL + "?template=bug_report.yml" + (m ? "&version=" + encodeURIComponent("v" + m[1]) : "");
    flashHint("Copied — paste it (⌘V) into the issue.");
    if (hasTauri()) await openExternal(url); else window.open(url, "_blank");
  }

  $("raiseIssue").addEventListener("click", raiseIssue);
  $("copyReport").addEventListener("click", copyReport);
  $("reportClose").addEventListener("click", closeReport);
  $("reportBackdrop").addEventListener("click", closeReport);
  $("reportIssue").addEventListener("click", reportIssue);
  $("revealReport").addEventListener("click", revealReport);

  return { open: raiseIssue };
}
