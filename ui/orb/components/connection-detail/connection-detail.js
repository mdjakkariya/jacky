/** Connection detail view — per-server tools list with per-tool risk/enable controls.
 *  Module pattern (not a custom element), created imperatively like add-connection.
 *  Exports showConnectionDetail(container, serverId, serverMeta, {onClose}) and
 *  hideConnectionDetail(container). */
import { daemon } from "../../lib/daemon.js";

// ---------------------------------------------------------------------------
// Risk cycle
// ---------------------------------------------------------------------------

/** Ordered risk levels used for cycling. */
const RISK_CYCLE = ["read_only", "write", "destructive"];

/**
 * Advance risk to the next level in the cycle.
 * Network tools are floored at "write" — if the computed next value would be
 * "read_only", it is bumped up to "write" instead.
 * @param {string} current  Current risk string (read_only / write / destructive)
 * @param {boolean} network Whether the tool is a network tool
 * @returns {string} The next risk string
 */
function nextRisk(current, network) {
  const idx = RISK_CYCLE.indexOf(current);
  const next = RISK_CYCLE[(idx + 1) % RISK_CYCLE.length];
  // Network floor: cannot drop below write
  if (network && next === "read_only") return "write";
  return next;
}

/** Map API risk string to CSS pill class. */
function riskClass(risk) {
  if (risk === "read_only") return "read";
  if (risk === "destructive") return "danger";
  return "write"; // "write" → "write"
}

/** Human-readable label for a risk level. */
function riskLabel(risk) {
  if (risk === "read_only") return "read";
  if (risk === "destructive") return "danger";
  return "write";
}

// ---------------------------------------------------------------------------
// DOM helpers (no innerHTML — safe construction only)
// ---------------------------------------------------------------------------

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function div(cls, text) { return el("div", cls, text); }
function btn(cls, text) { return el("button", cls, text); }
function span(cls, text) { return el("span", cls, text); }

// ---------------------------------------------------------------------------
// Tool row builder
// ---------------------------------------------------------------------------

/**
 * Build a single .tool-row DOM node for a tool descriptor.
 * @param {string} serverId   Server id (passed through to override calls)
 * @param {{name,description,risk,network,enabled}} tool
 * @returns {HTMLDivElement}
 */
function buildToolRow(serverId, tool) {
  const row = div("tool-row");
  row.dataset.toolName = tool.name;

  // Meta column
  const meta = div("tool-meta");
  const nameEl = div("tool-name", tool.name);
  const descEl = div("tool-desc", tool.description);
  meta.appendChild(nameEl);
  meta.appendChild(descEl);

  // Pill cluster (risk pill + optional network badge)
  const pillCluster = div("tool-pills");

  // Risk pill — clickable to cycle
  const riskPill = span("pill " + riskClass(tool.risk), riskLabel(tool.risk));
  riskPill.dataset.risk = tool.risk;
  riskPill.style.cursor = "pointer";
  riskPill.addEventListener("click", () => {
    const current = riskPill.dataset.risk;
    const next = nextRisk(current, !!tool.network);
    riskPill.dataset.risk = next;
    riskPill.className = "pill " + riskClass(next);
    riskPill.textContent = riskLabel(next);
    daemon.setMcpToolOverride(serverId, tool.name, { risk: next });
  });
  pillCluster.appendChild(riskPill);

  // Network badge (if network tool)
  if (tool.network) {
    const netBadge = span("pill net", "↗");
    pillCluster.appendChild(netBadge);
  }

  // Enable toggle
  const label = el("label", "switch");
  const checkbox = el("input");
  checkbox.type = "checkbox";
  checkbox.checked = !!tool.enabled;
  checkbox.addEventListener("change", () => {
    daemon.setMcpToolOverride(serverId, tool.name, { enabled: checkbox.checked });
  });
  label.appendChild(checkbox);
  label.appendChild(el("span", "slider"));  // the visible knob; CSS hides the input itself

  row.appendChild(meta);
  row.appendChild(pillCluster);
  row.appendChild(label);

  return row;
}

// ---------------------------------------------------------------------------
// Tools list renderer (called on initial load and re-sync)
// ---------------------------------------------------------------------------

/**
 * Load tools for the server and render them into toolsSection.
 * Updates the tools-count label in the section header.
 * @param {string} serverId
 * @param {HTMLElement} toolsSection  Container that will hold the tool rows
 * @param {HTMLElement} toolsLabel    The "Tools · N — …" label element
 */
async function loadTools(serverId, toolsSection, toolsLabel) {
  toolsSection.textContent = "Loading tools…";
  let tools = [];
  try {
    tools = await daemon.mcpTools(serverId);
    if (!Array.isArray(tools)) tools = [];
  } catch (_) {
    toolsSection.textContent = "Couldn't load tools.";
    return;
  }
  // Update label count
  if (toolsLabel) {
    toolsLabel.textContent = "";
    const countText = document.createTextNode("Tools · " + tools.length);
    const suffix = span("", " — toggle off any you don’t want Jack to use");
    suffix.style.textTransform = "none";
    suffix.style.letterSpacing = "0";
    suffix.style.color = "var(--muted)";
    toolsLabel.appendChild(countText);
    toolsLabel.appendChild(suffix);
  }
  toolsSection.textContent = "";
  tools.forEach((tool) => toolsSection.appendChild(buildToolRow(serverId, tool)));
}

// ---------------------------------------------------------------------------
// Status dot helper
// ---------------------------------------------------------------------------

/** Apply a final state to the header status dot after the debounce resolves. */
function applyHeaderStatus(card, state) {
  const dot = card.querySelector(".detail-status-dot");
  if (!dot) return;
  dot.className = "status-dot detail-status-dot " + state;
}

// ---------------------------------------------------------------------------
// Main exports
// ---------------------------------------------------------------------------

/**
 * Mount the connection-detail view into container for the given server.
 * @param {HTMLElement} container
 * @param {string} serverId
 * @param {{label,egress,state,auth_type}} serverMeta
 * @param {{onClose: function}} options
 * @returns {HTMLElement} The card element
 */
export function showConnectionDetail(container, serverId, serverMeta, { onClose }) {
  // Remove any existing detail card
  hideConnectionDetail(container);

  const card = div("connection-detail-card");

  // Debounce state for mcp_status churn tolerance (1500 ms)
  let debounceTimer = null;

  // -------------------------------------------------------------------------
  // Header row: icon + name + status dot + "Sign out" button
  // -------------------------------------------------------------------------
  const header = div("detail-header");

  const iconEl = div("srv-icon", serverMeta.icon || "🔌");
  header.appendChild(iconEl);

  const headerMeta = div("srv-meta");
  const nameRow = div("srv-name");
  const nameText = span("", serverMeta.label || serverId);
  nameRow.appendChild(nameText);
  headerMeta.appendChild(nameRow);

  const descRow = div("srv-desc");
  const dot = span("status-dot detail-status-dot " + (serverMeta.state || "disconnected"));
  const descText = span("", serverMeta.state === "connected" ? "Connected" : serverMeta.state || "Disconnected");
  descRow.appendChild(dot);
  descRow.appendChild(descText);
  headerMeta.appendChild(descRow);

  header.appendChild(headerMeta);

  // Sign out button
  const signOutBtn = btn("btn ghost-sm btn-signout", "Sign out");
  signOutBtn.addEventListener("click", async () => {
    const authType = serverMeta.auth_type || "";
    if (authType === "token") {
      // Clear the stored token
      await daemon.mcpSetToken(serverId, "");
    } else if (authType === "oauth") {
      // OAuth not yet supported — show note and call mcpAuthStart for consistency
      try {
        await daemon.mcpAuthStart(serverId);
      } catch (_) {}
      // Show a transient "coming soon" note below the sign-out button
      const existing = card.querySelector(".signout-note");
      if (!existing) {
        const note = div("signout-note");
        note.textContent = "OAuth sign-out coming in Phase 6 — not yet supported.";
        signOutBtn.insertAdjacentElement
          ? signOutBtn.parentNode.insertBefore(note, signOutBtn.nextSibling)
          : card.appendChild(note);
      }
    }
  });
  header.appendChild(signOutBtn);

  card.appendChild(header);

  // -------------------------------------------------------------------------
  // MCP banner (egress vs local)
  // -------------------------------------------------------------------------
  const banner = div(serverMeta.egress ? "mcp-banner" : "mcp-banner local");
  const bannerIcon = span("", serverMeta.egress ? "↗" : "●");
  const bannerTextEl = div("");
  if (serverMeta.egress) {
    const bold = el("strong", "", "This connection sends data off-device.");
    const suffix = document.createTextNode(" Every call is recorded in your audit log.");
    bannerTextEl.appendChild(bold);
    bannerTextEl.appendChild(suffix);
  } else {
    bannerTextEl.textContent = "All tools run on-device only. No data leaves your Mac.";
  }
  banner.appendChild(bannerIcon);
  banner.appendChild(bannerTextEl);
  card.appendChild(banner);

  // -------------------------------------------------------------------------
  // Section label + tools list
  // -------------------------------------------------------------------------
  const toolsLabel = div("secthead detail-tools-label", "Tools · …");
  card.appendChild(toolsLabel);

  const toolsSection = div("detail-tools-section");
  card.appendChild(toolsSection);

  // -------------------------------------------------------------------------
  // Danger zone
  // -------------------------------------------------------------------------
  const dzHead = div("secthead", "Danger zone");
  card.appendChild(dzHead);

  const dzRow = div("danger-zone");

  const removeBtn = btn("btn danger", "Remove connection");
  removeBtn.addEventListener("click", async () => {
    await daemon.removeMcpServer(serverId);
    onClose();
  });

  const resyncBtn = btn("btn ghost-sm", "Re-sync tools");
  resyncBtn.addEventListener("click", async () => {
    await daemon.connectMcpServer(serverId);
    await loadTools(serverId, toolsSection, toolsLabel);
  });

  dzRow.appendChild(removeBtn);
  dzRow.appendChild(resyncBtn);
  card.appendChild(dzRow);

  // -------------------------------------------------------------------------
  // Wire into container before async load (so tests can query the card)
  // -------------------------------------------------------------------------
  container.appendChild(card);

  // -------------------------------------------------------------------------
  // Subscribe to mcp_status WS events (churn-tolerance debounce)
  // -------------------------------------------------------------------------
  const offStatus = daemon.on("mcp_status", (msg) => {
    if (msg.server !== serverId) return;
    // Cancel any pending debounce
    if (debounceTimer !== null) clearTimeout(debounceTimer);
    // Immediately show reconnecting state
    const d = card.querySelector(".detail-status-dot");
    if (d) d.className = "status-dot detail-status-dot reconnecting";
    const dl = card.querySelector(".srv-desc span:last-child");
    if (dl) dl.textContent = "reconnecting…";
    // Arm 1500 ms timer to apply the final state
    debounceTimer = setTimeout(() => {
      debounceTimer = null;
      applyHeaderStatus(card, msg.state);
      const dl2 = card.querySelector(".srv-desc span:last-child");
      if (dl2) dl2.textContent = msg.state === "connected" ? "Connected" : msg.state || "Disconnected";
    }, 1500);
  });

  // Store unsubscribe fn on card so hideConnectionDetail can clean up
  card._offStatus = offStatus;
  card._debounceTimer = () => debounceTimer;
  card._clearDebounce = () => { if (debounceTimer !== null) { clearTimeout(debounceTimer); debounceTimer = null; } };

  // Kick off the initial async tools load
  loadTools(serverId, toolsSection, toolsLabel);

  return card;
}

/**
 * Remove the connection-detail card from container if present.
 * Also unsubscribes the mcp_status listener and clears any pending debounce.
 * @param {HTMLElement} container
 */
export function hideConnectionDetail(container) {
  const existing = container.querySelector(".connection-detail-card");
  if (!existing) return;
  if (typeof existing._clearDebounce === "function") existing._clearDebounce();
  if (typeof existing._offStatus === "function") existing._offStatus();
  existing.remove();
}
