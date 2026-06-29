/** MCP connections list: a card per server with status dot, egress badge, toggle,
 *  and reconnect-churn tolerance (1500 ms debounce on mcp_status events). */
import { daemon } from "../../lib/daemon.js";

/** Extract a display hostname for a network-egress server.
 *  Prefers srv.url (parsed via URL), falls back to the bare server id. */
function egressHost(srv) {
  // The server object may have a `url` field; use it to get a clean hostname.
  if (srv.url) {
    try { return new URL(srv.url).hostname; } catch (_) {}
  }
  // Fall back: use the bare server id (no synthetic ".com" suffix).
  return srv.server;
}

/** Build the description text for a card given current state / tool_count. */
function descText(state, tool_count, auth_type) {
  if (state === "auth_needed") return "Sign-in needed";
  if (state === "connected") return `Connected · ${tool_count} tools · ${auth_type}`;
  return "Disconnected";
}

export class ConnectionsList extends HTMLElement {
  #loading = false;
  #offStatus = null;   // WS unsubscribe fn
  #debounce = new Map(); // serverId -> timeoutId

  connectedCallback() {
    this.#offStatus = daemon.on("mcp_status", (msg) => this.#onMcpStatus(msg));
  }

  disconnectedCallback() {
    if (this.#offStatus) { this.#offStatus(); this.#offStatus = null; }
    // Cancel any pending debounce timers
    this.#debounce.forEach((tid) => clearTimeout(tid));
    this.#debounce.clear();
  }

  async load() {
    if (this.#loading) return;
    this.#loading = true;
    let servers = [];
    try {
      const res = await daemon.mcpServers();
      if (res && res.ok === false) {
        // MCP not enabled (or daemon not restarted since enabling) — guide the user.
        this.textContent = "";
        const msg = document.createElement("div");
        msg.className = "srv-desc";
        msg.style.padding = "12px 14px";
        msg.textContent = (res.error || "").toLowerCase().includes("disabled")
          ? "MCP is off. Turn on “Enable MCP connections” above to use connections."
          : (res.error || "Couldn’t load connections.");
        this.appendChild(msg);
        return;
      }
      servers = res.servers || [];
    } catch (_) {
      const err = document.createElement("div");
      err.className = "srv-desc";
      err.textContent = "Couldn’t load — is Jack running?";
      this.textContent = "";
      this.appendChild(err);
      this.#loading = false;
      return;
    } finally {
      this.#loading = false;
    }
    // Clear after await so concurrent calls cannot stack rows
    this.textContent = "";
    servers.forEach((srv) => this.appendChild(this.#renderCard(srv)));
    // Add connection button row
    const row = document.createElement("div");
    row.className = "add-conn-row";
    const btn = document.createElement("button");
    btn.className = "btn primary add-conn-btn";
    btn.textContent = "+ Add connection";
    btn.addEventListener("click", () => {
      this.dispatchEvent(new CustomEvent("add-connection", { bubbles: true }));
    });
    row.appendChild(btn);
    this.appendChild(row);
  }

  /** Build and return a div.srv-card DOM node for a server descriptor. */
  #renderCard(srv) {
    const card = document.createElement("div");
    card.className = "srv-card";
    card.dataset.serverId = srv.server;
    // Stash auth_type on the card so status updates (which don't carry it, and which
    // overwrite the description with "reconnecting…") can restore it without scraping text.
    card.dataset.authType = srv.auth_type || "";

    // Icon
    const icon = document.createElement("div");
    icon.className = "srv-icon";
    icon.textContent = srv.icon || "?";

    // Meta column
    const meta = document.createElement("div");
    meta.className = "srv-meta";

    // Name row: label + egress pill
    const nameRow = document.createElement("div");
    nameRow.className = "srv-name";
    const nameSpan = document.createElement("span");
    nameSpan.textContent = srv.label;
    nameRow.appendChild(nameSpan);

    const pill = document.createElement("span");
    if (srv.egress === "network") {
      pill.className = "pill net";
      pill.textContent = "↗ sends to " + egressHost(srv);
    } else {
      pill.className = "pill local";
      pill.textContent = "● on-device";
    }
    nameRow.appendChild(pill);
    meta.appendChild(nameRow);

    // Description row: status dot + text
    const descRow = document.createElement("div");
    descRow.className = "srv-desc";

    const dot = document.createElement("span");
    dot.className = "status-dot " + srv.state;
    descRow.appendChild(dot);

    const descLabel = document.createElement("span");
    descLabel.textContent = descText(srv.state, srv.tool_count, srv.auth_type);
    descRow.appendChild(descLabel);
    meta.appendChild(descRow);

    // Toggle
    const label = document.createElement("label");
    label.className = "switch";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = !!srv.enabled;
    checkbox.addEventListener("click", (e) => {
      // Prevent click from bubbling to card (would also fire server-select)
      e.stopPropagation();
      if (checkbox.checked) {
        daemon.enableMcpServer(srv.server);
      } else {
        daemon.disableMcpServer(srv.server);
      }
    });
    label.appendChild(checkbox);
    const slider = document.createElement("span");
    slider.className = "slider";  // the visible knob; CSS hides the input itself
    label.appendChild(slider);

    card.appendChild(icon);
    card.appendChild(meta);
    card.appendChild(label);

    // Clicking the meta column (not the toggle) emits server-select with the full row.
    meta.addEventListener("click", () => {
      this.dispatchEvent(new CustomEvent("server-select", { bubbles: true, detail: srv }));
    });

    return card;
  }

  /** Apply a received mcp_status payload to the matching rendered card.
   *  Called immediately (shows "reconnecting…") and again after the debounce. */
  #applyStatus(id, state, tool_count) {
    const card = this.querySelector(`.srv-card[data-server-id="${id}"]`);
    if (!card) return;
    const dot = card.querySelector(".status-dot");
    const descLabel = card.querySelectorAll(".srv-desc span")[1];
    const auth_type = card.dataset.authType || "";  // stable; never scraped from text
    if (dot) {
      dot.className = "status-dot " + state;
    }
    if (descLabel) {
      descLabel.textContent = descText(state, tool_count, auth_type);
    }
  }

  /** Handle a raw mcp_status WS message with churn-tolerance debounce. */
  #onMcpStatus(msg) {
    const { server: id, state, tool_count } = msg;
    const card = this.querySelector(`.srv-card[data-server-id="${id}"]`);
    if (!card) return;

    // Cancel any pending timer for this server
    const existing = this.#debounce.get(id);
    if (existing !== undefined) clearTimeout(existing);

    // Immediately show "reconnecting…" transient state
    const dot = card.querySelector(".status-dot");
    const descLabel = card.querySelectorAll(".srv-desc span")[1];
    if (dot) dot.className = "status-dot reconnecting";
    if (descLabel) descLabel.textContent = "reconnecting…";

    // Arm 1500 ms timer to apply the final received state
    const tid = setTimeout(() => {
      this.#debounce.delete(id);
      this.#applyStatus(id, state, tool_count);
    }, 1500);
    this.#debounce.set(id, tid);
  }
}

customElements.define("connections-list", ConnectionsList);
