/** Add-connection wizard — 4-step modal.
 *  Module pattern (not a custom element), created imperatively like confirm-card.
 *  Exports showAddConnection(container, {onDone, onCancel}) and hideAddConnection(container). */
import { daemon } from "../../lib/daemon.js";

/** Static catalog of known MCP servers. URLs are the providers' official hosted
 *  MCP endpoints (verified June 2026); the wizard's URL field stays editable so a
 *  user can change one if a provider moves theirs. */
const CATALOG = [
  { id: "slack",  label: "Slack",       icon: "💬", transport: "http",  url: "https://mcp.slack.com/mcp",          auth: "oauth", egress: true,  desc: "OAuth" },
  { id: "github", label: "GitHub",      icon: "🐙", transport: "http",  url: "https://api.githubcopilot.com/mcp/", auth: "token", egress: true,  desc: "Personal Access Token" },
  { id: "files",  label: "Local Files", icon: "📁", transport: "stdio", command: "npx @mcp/server-files",          auth: "none",  egress: false, desc: "on-device" },
  { id: "notion", label: "Notion",      icon: "🗒️", transport: "http",  url: "https://mcp.notion.com/mcp",         auth: "oauth", egress: true,  desc: "OAuth" },
];

const CUSTOM_ENTRY = {
  id: "custom", label: "Custom MCP server", icon: "➕",
  desc: "Paste a command (stdio) or an https URL — no code needed",
  custom: true,
};

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

// ---------------------------------------------------------------------------
// Step bar
// ---------------------------------------------------------------------------

/** Render the 4-segment progress bar. step is 1-based current step. */
function renderStepBar(step) {
  const bar = div("wizard-steps");
  for (let i = 1; i <= 4; i++) {
    const seg = div("wizard-step");
    if (i < step) seg.classList.add("done");
    else if (i === step) seg.classList.add("now");
    bar.appendChild(seg);
  }
  return bar;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/** Mutable wizard state shared across steps. */
function makeState() {
  return {
    step: 1,
    // Source selection
    catalogEntry: null,   // CATALOG entry or CUSTOM_ENTRY
    // Transport
    transport: "http",    // "http" | "stdio"
    url: "",
    command: "",
    args: "",
    env: "",
    // Auth
    authType: "oauth",    // "oauth" | "token" | "none"
    // Token
    token: "",
  };
}

// ---------------------------------------------------------------------------
// Step 1 — Source
// ---------------------------------------------------------------------------

function renderStep1(state, callbacks) {
  const wrap = div("wizard-body");
  wrap.dataset.step = "1";

  wrap.appendChild(renderStepBar(1));

  // "From catalog" section
  wrap.appendChild(div("secthead", "From catalog"));
  const grid = div("catalog-grid");
  CATALOG.forEach((entry) => {
    grid.appendChild(buildCatItem(entry, state));
  });
  wrap.appendChild(grid);

  // "Or add your own" section
  wrap.appendChild(div("secthead", "Or add your own"));
  const customItem = buildCatItem(CUSTOM_ENTRY, state);
  customItem.style.gridColumn = "1 / -1";
  wrap.appendChild(customItem);

  // Buttons row
  const row = div("wizard-row");
  const cancelBtn = btn("btn btn-cancel", "Cancel");
  const spacer = div("spacer");
  const nextBtn = btn("btn primary btn-next", "Continue");

  cancelBtn.addEventListener("click", () => callbacks.onCancel());
  nextBtn.addEventListener("click", () => {
    if (!state.catalogEntry) return; // nothing selected — no-op
    callbacks.goTo(2);
  });

  row.appendChild(cancelBtn);
  row.appendChild(spacer);
  row.appendChild(nextBtn);
  wrap.appendChild(row);

  return wrap;
}

/** Build a single catalog item div. */
function buildCatItem(entry, state) {
  const item = div("cat-item");
  const iconEl = div("cat-icon", entry.icon);
  const meta = div("");
  const name = div("cat-name", entry.label);
  const desc = div("cat-desc", entry.desc);
  meta.appendChild(name);
  meta.appendChild(desc);
  item.appendChild(iconEl);
  item.appendChild(meta);

  item.addEventListener("click", () => {
    // Deselect all siblings — look up in parent context
    const root = item.closest(".wizard-card");
    if (root) root.querySelectorAll(".cat-item").forEach((i) => i.classList.remove("selected"));
    item.classList.add("selected");
    state.catalogEntry = entry;
    if (!entry.custom) {
      state.transport = entry.transport;
      state.url = entry.url || "";
      state.command = entry.command || "";
      state.authType = entry.auth;
    } else {
      // Reset transport for custom to allow user choice
      state.transport = "http";
      state.url = "";
      state.command = "";
    }
  });

  return item;
}

// ---------------------------------------------------------------------------
// Step 2 — Transport
// ---------------------------------------------------------------------------

function renderStep2(state, callbacks) {
  const wrap = div("wizard-body");
  wrap.dataset.step = "2";

  wrap.appendChild(renderStepBar(2));

  const secHead = div("secthead", "Connection type");
  wrap.appendChild(secHead);

  const isCustom = state.catalogEntry && state.catalogEntry.custom;

  // Transport options (always visible; read-only for catalog, editable for custom)
  const httpOpt = buildOptItem("http", "Remote (Streamable HTTP)", "Connect via an https URL", state.transport === "http");
  const stdioOpt = buildOptItem("stdio", "Local (stdio subprocess)", "Run a local command on your Mac", state.transport === "stdio");

  httpOpt.dataset.transport = "http";
  stdioOpt.dataset.transport = "stdio";

  if (isCustom) {
    // Make them interactive for custom
    httpOpt.addEventListener("click", () => {
      state.transport = "http";
      httpOpt.classList.add("selected");
      stdioOpt.classList.remove("selected");
      refreshTransportFields(wrap, state);
    });
    stdioOpt.addEventListener("click", () => {
      state.transport = "stdio";
      stdioOpt.classList.add("selected");
      httpOpt.classList.remove("selected");
      refreshTransportFields(wrap, state);
    });
  }

  wrap.appendChild(httpOpt);
  wrap.appendChild(stdioOpt);

  // Transport-specific fields
  const fieldsContainer = div("transport-fields");
  wrap.appendChild(fieldsContainer);
  renderTransportFields(fieldsContainer, state, isCustom);

  // Buttons
  const row = div("wizard-row");
  const backBtn = btn("btn btn-back", "Back");
  const spacer = div("spacer");
  const nextBtn = btn("btn primary btn-next", "Continue");

  backBtn.addEventListener("click", () => callbacks.goTo(1));
  nextBtn.addEventListener("click", () => {
    // Read current field values into state
    const urlField = wrap.querySelector("[data-field='url']");
    const cmdField = wrap.querySelector("[data-field='command']");
    const argsField = wrap.querySelector("[data-field='args']");
    const envField = wrap.querySelector("[data-field='env']");
    if (urlField) state.url = urlField.value;
    if (cmdField) state.command = cmdField.value;
    if (argsField) state.args = argsField.value;
    if (envField) state.env = envField.value;
    // Gate: an HTTP server always needs a URL (catalog entries may ship blank);
    // a custom stdio server needs a command.
    if (state.transport === "http" && !state.url.trim()) return;
    if (isCustom && state.transport === "stdio" && !state.command.trim()) return;
    callbacks.goTo(3);
  });

  row.appendChild(backBtn);
  row.appendChild(spacer);
  row.appendChild(nextBtn);
  wrap.appendChild(row);

  return wrap;
}

function buildOptItem(transportKey, title, desc, selected) {
  const item = div("opt-item" + (selected ? " selected" : ""));
  const radio = div("opt-radio");
  const meta = div("");
  const titleEl = div("opt-title", title);
  const descEl = div("opt-desc", desc);
  meta.appendChild(titleEl);
  meta.appendChild(descEl);
  item.appendChild(radio);
  item.appendChild(meta);
  return item;
}

function renderTransportFields(container, state, editable) {
  container.textContent = "";
  if (state.transport === "http") {
    const label = div("field-label", "Server URL");
    container.appendChild(label);
    const input = el("input", "field-input");
    input.type = "text";
    input.dataset.field = "url";
    input.value = state.url || "";
    input.placeholder = "https://…/mcp";
    // URL is always editable (even for catalog) so a prefilled official endpoint
    // can be corrected if a provider changes theirs.
    container.appendChild(input);
  } else {
    // stdio
    const cmdLabel = div("field-label", "Command");
    container.appendChild(cmdLabel);
    const cmdInput = el("input", "field-input");
    cmdInput.type = "text";
    cmdInput.dataset.field = "command";
    cmdInput.value = state.command || "";
    if (!editable) cmdInput.readOnly = true;
    container.appendChild(cmdInput);

    if (editable) {
      const argsLabel = div("field-label", "Arguments (space-separated)");
      container.appendChild(argsLabel);
      const argsInput = el("input", "field-input");
      argsInput.type = "text";
      argsInput.dataset.field = "args";
      argsInput.value = state.args || "";
      container.appendChild(argsInput);

      const envLabel = div("field-label", "Environment variables (KEY=VALUE, one per line)");
      container.appendChild(envLabel);
      const envArea = el("textarea", "field-input");
      envArea.dataset.field = "env";
      envArea.value = state.env || "";
      container.appendChild(envArea);
    }
  }
}

function refreshTransportFields(wrap, state) {
  const isCustom = state.catalogEntry && state.catalogEntry.custom;
  const container = wrap.querySelector(".transport-fields");
  if (container) renderTransportFields(container, state, isCustom);
}

// ---------------------------------------------------------------------------
// Step 3 — Auth
// ---------------------------------------------------------------------------

function renderStep3(state, callbacks) {
  const wrap = div("wizard-body");
  wrap.dataset.step = "3";

  wrap.appendChild(renderStepBar(3));

  // OAuth option
  const oauthOpt = buildOptItem("oauth", "Sign in with OAuth 2.1 (recommended)", "Opens your browser · scoped, revocable, auto-refreshing token", state.authType === "oauth");
  oauthOpt.dataset.authType = "oauth";

  // Token option
  const tokenOpt = buildOptItem("token", "Paste a bot token", "Stored only in your macOS Keychain — never on disk", state.authType === "token");
  tokenOpt.dataset.authType = "token";

  oauthOpt.addEventListener("click", () => {
    state.authType = "oauth";
    oauthOpt.classList.add("selected");
    tokenOpt.classList.remove("selected");
  });
  tokenOpt.addEventListener("click", () => {
    state.authType = "token";
    tokenOpt.classList.add("selected");
    oauthOpt.classList.remove("selected");
  });

  wrap.appendChild(oauthOpt);
  wrap.appendChild(tokenOpt);

  // Keychain banner (always shown)
  const banner = div("mcp-banner local keychain-banner");
  const bannerIcon = div("", "🔑");
  const bannerText = div("");
  const bannerLine1 = document.createTextNode("Whichever you choose, the credential lives in your ");
  const bannerBold = el("strong", "", "macOS Keychain");
  const bannerLine2 = document.createTextNode(" under autobot.secrets — never written to disk.");
  bannerText.appendChild(bannerLine1);
  bannerText.appendChild(bannerBold);
  bannerText.appendChild(bannerLine2);
  banner.appendChild(bannerIcon);
  banner.appendChild(bannerText);
  wrap.appendChild(banner);

  // Buttons
  const row = div("wizard-row");
  const backBtn = btn("btn btn-back", "Back");
  const spacer = div("spacer");
  const nextBtn = btn("btn primary btn-next", "Continue");

  backBtn.addEventListener("click", () => callbacks.goTo(2));
  nextBtn.addEventListener("click", () => callbacks.goTo(4));

  row.appendChild(backBtn);
  row.appendChild(spacer);
  row.appendChild(nextBtn);
  wrap.appendChild(row);

  return wrap;
}

// ---------------------------------------------------------------------------
// Step 4 — Final action (OAuth or Token)
// ---------------------------------------------------------------------------

function renderStep4(state, callbacks) {
  const wrap = div("wizard-body");
  wrap.dataset.step = "4";

  wrap.appendChild(renderStepBar(4));

  if (state.authType === "oauth") {
    renderOAuthExplainer(wrap, state, callbacks);
  } else {
    renderTokenForm(wrap, state, callbacks);
  }

  return wrap;
}

function renderOAuthExplainer(wrap, state, callbacks) {
  const explainer = div("oauth-explainer");

  // Emoji header
  const emojiRow = div("oauth-emoji-row", "💬 → 🌐");

  const desc = div("oauth-desc");
  desc.textContent = "Jack will open " + (state.catalogEntry ? state.catalogEntry.label : "the server") + " in your browser to sign in. It never sees your password.";

  // Data disclosure banner
  const banner = div("mcp-banner");
  const bannerIcon = div("", "↗");
  const bannerText = div("");
  const bold = el("strong", "", "What leaves your Mac after this: ");
  const text = document.createTextNode("the text of messages you ask Jack to send or search. Audio and your memory profile stay on-device.");
  bannerText.appendChild(bold);
  bannerText.appendChild(text);
  banner.appendChild(bannerIcon);
  banner.appendChild(bannerText);

  explainer.appendChild(emojiRow);
  explainer.appendChild(desc);
  explainer.appendChild(banner);
  wrap.appendChild(explainer);

  // Live status line (browser opening → waiting → connected / error).
  const statusEl = div("oauth-msg-placeholder");
  wrap.appendChild(statusEl);

  // Buttons
  const row = div("wizard-row");
  const backBtn = btn("btn btn-back", "Back");
  const spacer = div("spacer");
  const openBtn = btn("btn primary btn-open-browser", "Open browser");

  // OAuth runs on the daemon: registering + enabling the server connects it,
  // which triggers the SDK's browser hand-off. We subscribe to the mcp_oauth
  // stage events and mcp_status before kicking it off so no early stage is missed.
  let offOauth = null;
  let offStatus = null;
  let settled = false;
  function cleanup() {
    if (offOauth) { offOauth(); offOauth = null; }
    if (offStatus) { offStatus(); offStatus = null; }
  }
  function setStatus(text, kind) {
    statusEl.textContent = "";
    const s = div("oauth-status" + (kind && kind !== "progress" ? " " + kind : ""));
    if (kind === "progress") s.appendChild(div("oauth-spinner"));
    const t = document.createElement("span");
    t.textContent = text;
    s.appendChild(t);
    statusEl.appendChild(s);
  }

  backBtn.addEventListener("click", () => { cleanup(); callbacks.goTo(3); });
  openBtn.addEventListener("click", async () => {
    if (settled) return;
    openBtn.disabled = true;
    const descriptor = buildDescriptor(state);
    const id = descriptor.id;

    offOauth = daemon.on("mcp_oauth", (m) => {
      if (m.server !== id || settled) return;
      if (m.stage === "browser_open") setStatus("Opening your browser — sign in there…", "progress");
      else if (m.stage === "waiting_callback") setStatus("Waiting for you to finish signing in…", "progress");
      else if (m.stage === "callback_received") setStatus("Signing in…", "progress");
    });
    offStatus = daemon.on("mcp_status", (m) => {
      if (m.server !== id || settled) return;
      if (m.state === "connected") {
        settled = true; cleanup();
        setStatus("Connected!", "ok");
        setTimeout(() => callbacks.onDone(), 600);
      } else if (m.state === "error") {
        settled = true; cleanup();
        setStatus("Sign-in failed — " + (m.error || "check the server URL and try again."), "error");
        openBtn.disabled = false;
      }
    });

    try {
      await daemon.addMcpServer(descriptor);
      setStatus("Opening your browser — sign in there…", "progress");
      const res = await daemon.enableMcpServer(id); // connects → triggers the OAuth hand-off
      if (res && res.ok === false && !settled) {
        settled = true; cleanup();
        setStatus("Couldn't start sign-in — " + (res.error || "is Jack running?"), "error");
        openBtn.disabled = false;
      }
    } catch (e) {
      if (!settled) {
        settled = true; cleanup();
        setStatus("Couldn't start sign-in — " + ((e && e.message) || "check that Jack is running."), "error");
        openBtn.disabled = false;
      }
    }
  });

  row.appendChild(backBtn);
  row.appendChild(spacer);
  row.appendChild(openBtn);
  wrap.appendChild(row);
}

function renderTokenForm(wrap, state, callbacks) {
  const label = div("field-label", "Access token");
  const tokenInput = el("input", "field-input");
  tokenInput.type = "password";
  tokenInput.dataset.field = "token";
  tokenInput.placeholder = "Paste your token (e.g. GitHub PAT, Slack bot token)";

  wrap.appendChild(label);
  wrap.appendChild(tokenInput);

  const errPlaceholder = div("token-error-placeholder");
  wrap.appendChild(errPlaceholder);

  // Buttons
  const row = div("wizard-row");
  const backBtn = btn("btn btn-back", "Back");
  const spacer = div("spacer");
  const connectBtn = btn("btn primary btn-connect", "Connect");

  backBtn.addEventListener("click", () => callbacks.goTo(3));
  connectBtn.addEventListener("click", async () => {
    const token = tokenInput.value.trim();
    connectBtn.disabled = true;
    try {
      const descriptor = buildDescriptor(state);
      await daemon.addMcpServer(descriptor);
      await daemon.mcpSetToken(descriptor.id, token);
      await daemon.enableMcpServer(descriptor.id);
      callbacks.onDone();
    } catch (e) {
      const errMsg = div("token-error");
      errMsg.textContent = "Connection failed — " + (e && e.message ? e.message : "check the daemon.");
      errPlaceholder.textContent = "";
      errPlaceholder.appendChild(errMsg);
      connectBtn.disabled = false;
    }
  });

  row.appendChild(backBtn);
  row.appendChild(spacer);
  row.appendChild(connectBtn);
  wrap.appendChild(row);
}

// ---------------------------------------------------------------------------
// Descriptor builder
// ---------------------------------------------------------------------------

function buildDescriptor(state) {
  const entry = state.catalogEntry;
  const isCustom = entry && entry.custom;
  const id = isCustom ? "custom-" + Date.now() : entry.id;
  const label = isCustom ? "Custom" : entry.label;
  const egress = isCustom ? "local" : (entry.egress ? "network" : "local");

  const descriptor = {
    id,
    label,
    transport: state.transport,
    auth: { type: state.authType },
    egress,
  };

  // For token auth, point the descriptor at the Keychain account the wizard writes
  // (mcpSetToken stores under "mcp.<id>.token"). Without this the worker has no
  // secret_ref and never sends the bearer header / env token.
  if (state.authType === "token") {
    descriptor.secret_ref = "mcp." + id + ".token";
  }

  if (state.transport === "http") {
    descriptor.url = state.url;
  } else {
    descriptor.command = state.command;
    descriptor.args = state.args ? state.args.trim().split(/\s+/) : [];
    const envLines = (state.env || "").split("\n");
    descriptor.env = Object.fromEntries(
      envLines
        .map((l) => l.trim())
        .filter((l) => l.includes("="))
        .map((l) => { const i = l.indexOf("="); return [l.slice(0, i).trim(), l.slice(i + 1).trim()]; })
    );
  }

  return descriptor;
}

// ---------------------------------------------------------------------------
// Wizard orchestrator
// ---------------------------------------------------------------------------

/** Mount the wizard into container. Returns the card element. */
export function showAddConnection(container, { onDone, onCancel }) {
  // Remove any existing wizard
  hideAddConnection(container);

  const card = div("wizard-card");
  container.appendChild(card);

  const state = makeState();

  function goTo(step) {
    state.step = step;
    card.textContent = "";
    const callbacks = { onDone, onCancel, goTo };
    let stepEl;
    if (step === 1) stepEl = renderStep1(state, callbacks);
    else if (step === 2) stepEl = renderStep2(state, callbacks);
    else if (step === 3) stepEl = renderStep3(state, callbacks);
    else stepEl = renderStep4(state, callbacks);
    card.appendChild(stepEl);
  }

  goTo(1);
  return card;
}

/** Remove the wizard from container if present. */
export function hideAddConnection(container) {
  const existing = container.querySelector(".wizard-card");
  if (existing) existing.remove();
}
