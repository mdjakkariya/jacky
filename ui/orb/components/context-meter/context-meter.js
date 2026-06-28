/** Per-session context meter in the chat header (percent ring + tap-to-expand detail
 *  card). Controller module (not a custom element): the ring #ctx and the detail card
 *  #ctxDetail are separate, positioned elements. Returns { update, reset }. Moved from
 *  chat.html. */
import { $, pointCaretAt } from "../../lib/dom.js";
import { fmtK, fmtModel, fmtUSD } from "../../lib/format.js";
import { registerPopover, closeOtherPopovers } from "../../lib/popover.js";

const CTX_CIRC = 94.2;
const CTX_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 14l4-4"/><path d="M5.6 18.5a9 9 0 1 1 12.8 0"/></svg>';

export function setupContextMeter() {
  let lastCtx = null;

  function update(m) {
    const wrap = $("ctx"), arc = $("ctxArc"), pctEl = $("ctxPct");
    if (!wrap) return;
    lastCtx = m;
    const p = Math.max(0, Math.min(100, Math.round(m.pct || 0)));
    const c = p > 85 ? "var(--danger)" : (p > 60 ? "#e7b15a" : "#7fb87a");
    wrap.classList.add("show");
    arc.setAttribute("stroke", c);
    arc.setAttribute("stroke-dashoffset", (CTX_CIRC * (1 - p / 100)).toFixed(1));
    pctEl.textContent = p + "%";
    pctEl.style.color = p > 85 ? "var(--danger)" : "var(--muted)";
    wrap.title = "Context " + p + "% used this session — tap for detail";
    if (!$("ctxDetail").classList.contains("hidden")) renderDetail(); // live-update if open
  }

  function renderDetail() {
    const d = $("ctxDetail"); if (!lastCtx) return;
    const p = Math.max(0, Math.min(100, Math.round(lastCtx.pct || 0)));
    let rows = '<div class="t">' + CTX_ICON + '<span>Session context</span></div>'
      + '<div class="r"><span>Used</span><b>' + fmtK(lastCtx.used) + ' / ' + fmtK(lastCtx.window) + '</b></div>'
      + '<div class="r"><span>Window</span><b>' + p + '%</b></div>'
      + '<div class="r"><span>This turn</span><b>' + fmtK(lastCtx.turn_in) + ' in · ' + fmtK(lastCtx.turn_out) + ' out</b></div>';
    if (lastCtx.cache_read != null || lastCtx.cache_write != null) { // cloud only
      rows += '<div class="r"><span>Cache read</span><b>' + fmtK(lastCtx.cache_read) + '</b></div>'
        + '<div class="r"><span>Cache write</span><b>' + fmtK(lastCtx.cache_write) + '</b></div>';
    }
    if (lastCtx.price != null) { // cloud with a known list price (local sends null -> row hidden)
      rows += '<div class="r"><span>Session cost</span><b>' + fmtUSD(lastCtx.price) + '</b></div>';
    }
    rows += '<div class="r"><span>Model</span><b>' + fmtModel(lastCtx.model) + '</b></div>';
    const note = p > 85 ? "Compacting soon." : (p > 60 ? "Filling up." : "Plenty of room.");
    rows += '<div class="note">' + note + '</div>';
    d.innerHTML = rows;
  }

  function reset() {
    lastCtx = null;
    const wrap = $("ctx"), arc = $("ctxArc"), pctEl = $("ctxPct");
    if (wrap) { wrap.classList.remove("show"); } // hide entirely until the next turn
    if (arc) { arc.setAttribute("stroke-dashoffset", CTX_CIRC.toFixed(1)); arc.setAttribute("stroke", "#7fb87a"); }
    if (pctEl) { pctEl.textContent = "0%"; pctEl.style.color = "var(--muted)"; }
    $("ctxDetail").classList.add("hidden");
  }

  function closeDetail() { const d = $("ctxDetail"); if (d) d.classList.add("hidden"); }
  registerPopover(closeDetail); // let other popovers close this one when they open

  const ctx = $("ctx");
  if (ctx) ctx.addEventListener("click", () => {
    if (!lastCtx) return;
    const d = $("ctxDetail");
    if (d.classList.contains("hidden")) {
      closeOtherPopovers(closeDetail); // only one popover open at a time
      d.classList.remove("hidden");
      renderDetail();
      pointCaretAt(d, ctx); // aim the caret at the ring
    } else {
      d.classList.add("hidden");
    }
  });

  return { update, reset };
}
