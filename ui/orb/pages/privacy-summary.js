/** Privacy summary: inventories all active off-device data exits and renders them.
 *  Exported as pure functions so they can be unit-tested without DOM. */

/**
 * Compute the list of active off-device exits from settings + mcp servers.
 *
 * @param {object} settings - Result of daemon.settings() (may include llm_provider, allow_web).
 * @param {object[]} servers - Array of server descriptors from daemon.mcpServers().servers.
 * @returns {{ icon: string, name: string, desc: string }[]} Active exits in display order.
 */
export function privacyExits(settings, servers) {
  const exits = [];

  // Web search exit
  if (settings.allow_web) {
    exits.push({
      icon: "🔎",
      name: "Web search",
      desc: "Sends only your search query",
    });
  }

  // Cloud LLM exit
  if (settings.llm_provider === "anthropic") {
    exits.push({
      icon: "🧠",
      name: "Cloud LLM (Anthropic)",
      desc: "Sends conversation + memory profile + tool results",
    });
  }

  // Enabled network-egress MCP servers
  for (const srv of servers) {
    if (srv.enabled && srv.egress === "network") {
      let host = "";
      if (srv.url) {
        try {
          host = new URL(srv.url).hostname;
        } catch (_) {
          host = srv.server;
        }
      } else {
        host = srv.server;
      }
      exits.push({
        icon: srv.icon || "🔌",
        name: srv.label || srv.server,
        desc: `Sends data to ${host}`,
      });
    }
  }

  return exits;
}

/**
 * Render the privacy summary into `el`, given exits inventory.
 * Uses the CSS tokens defined in settings.css: .mcp-banner, .exit-row, .exit-icon,
 * .exit-meta, .exit-name, .exit-desc, button.btn.ghost-sm.
 * Separated from privacyExits() so DOM rendering is not under test.
 *
 * @param {HTMLElement} el - Container element to render into.
 * @param {{ icon: string, name: string, desc: string }[]} exits - Exit descriptors.
 * @param {() => void} onViewAuditLog - Called when "View audit log →" is clicked.
 */
export function renderPrivacySummary(el, exits, onViewAuditLog) {
  // Clear previous render (idempotent re-render support).
  while (el.firstChild) el.removeChild(el.firstChild);

  // Privacy banner (.mcp-banner.local per settings.css)
  const banner = document.createElement("div");
  banner.className = "mcp-banner local";
  const bannerStrong = document.createElement("strong");
  bannerStrong.textContent = "By default, everything runs on your Mac.";
  const bannerBody = document.createElement("div");
  bannerBody.appendChild(bannerStrong);
  bannerBody.appendChild(document.createTextNode(" The items below are the only ways data can leave it. All are opt-in."));
  banner.appendChild(bannerBody);
  el.appendChild(banner);

  // Section heading
  const head = document.createElement("div");
  head.className = "label";
  head.style.padding = "10px 14px 4px";
  head.textContent = `Active off-device exits · ${exits.length}`;
  el.appendChild(head);

  if (exits.length === 0) {
    const none = document.createElement("div");
    none.className = "hint";
    none.style.padding = "8px 14px 12px";
    none.textContent = "No off-device exits are active. Everything stays on your Mac.";
    el.appendChild(none);
  } else {
    // Exit rows — using .exit-row / .exit-icon / .exit-meta / .exit-name / .exit-desc
    for (const exit of exits) {
      const row = document.createElement("div");
      row.className = "exit-row";

      const icon = document.createElement("div");
      icon.className = "exit-icon";
      icon.textContent = exit.icon;

      const meta = document.createElement("div");
      meta.className = "exit-meta";

      const name = document.createElement("div");
      name.className = "exit-name";
      name.textContent = exit.name;

      const desc = document.createElement("div");
      desc.className = "exit-desc";
      desc.textContent = exit.desc;

      meta.appendChild(name);
      meta.appendChild(desc);
      row.appendChild(icon);
      row.appendChild(meta);
      el.appendChild(row);
    }
  }

  // Audit log link row
  const linkRow = document.createElement("div");
  linkRow.className = "row";
  linkRow.style.padding = "6px 14px 2px";
  const spacer = document.createElement("span");
  spacer.className = "spacer";
  const auditLink = document.createElement("button");
  auditLink.className = "btn ghost-sm";
  auditLink.textContent = "View audit log →";
  auditLink.addEventListener("click", onViewAuditLog);
  linkRow.appendChild(spacer);
  linkRow.appendChild(auditLink);
  el.appendChild(linkRow);
}
