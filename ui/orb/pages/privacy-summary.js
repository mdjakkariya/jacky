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
          host = srv.server + ".com";
        }
      } else {
        host = srv.server + ".com";
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
 * Separated from privacyExits() so DOM rendering is not under test.
 *
 * @param {HTMLElement} el - Container element to render into.
 * @param {{ icon: string, name: string, desc: string }[]} exits - Exit descriptors.
 * @param {() => void} onViewAuditLog - Called when "View audit log →" is clicked.
 */
export function renderPrivacySummary(el, exits, onViewAuditLog) {
  el.innerHTML = "";

  // Privacy banner
  const banner = document.createElement("div");
  banner.className = "banner local";
  banner.style.marginBottom = "14px";
  const bannerIcon = document.createElement("span");
  bannerIcon.className = "banner-icon";
  bannerIcon.textContent = "🔒";
  const bannerText = document.createElement("div");
  const bannerStrong = document.createElement("strong");
  bannerStrong.textContent = "By default, everything runs on your Mac.";
  bannerText.appendChild(bannerStrong);
  bannerText.appendChild(document.createTextNode(" The items below are the only ways data can leave it. All are opt-in."));
  banner.appendChild(bannerIcon);
  banner.appendChild(bannerText);
  el.appendChild(banner);

  // Section heading
  const head = document.createElement("div");
  head.className = "secthead";
  head.textContent = `Active off-device exits · ${exits.length}`;
  el.appendChild(head);

  if (exits.length === 0) {
    const none = document.createElement("div");
    none.className = "hint";
    none.style.margin = "8px 0 12px";
    none.textContent = "No off-device exits are active. Everything stays on your Mac.";
    el.appendChild(none);
  } else {
    // Exit rows
    for (const exit of exits) {
      const row = document.createElement("div");
      row.className = "exit";

      const icon = document.createElement("div");
      icon.className = "ei";
      icon.textContent = exit.icon;

      const text = document.createElement("div");
      text.className = "et";

      const name = document.createElement("div");
      name.className = "en";
      name.textContent = exit.name;

      const desc = document.createElement("div");
      desc.className = "ed";
      desc.textContent = exit.desc;

      text.appendChild(name);
      text.appendChild(desc);
      row.appendChild(icon);
      row.appendChild(text);
      el.appendChild(row);
    }
  }

  // Audit log link row
  const linkRow = document.createElement("div");
  linkRow.className = "row";
  linkRow.style.marginTop = "6px";
  const spacer = document.createElement("span");
  spacer.className = "spacer";
  const auditLink = document.createElement("button");
  auditLink.className = "btn ghost sm";
  auditLink.textContent = "View audit log →";
  auditLink.addEventListener("click", onViewAuditLog);
  linkRow.appendChild(spacer);
  linkRow.appendChild(auditLink);
  el.appendChild(linkRow);
}
