# MCP Phase 5 — UI Implementation Plan

**Date:** 2026-06-29
**Branch:** `feat/mcp-integration`
**Scope:** Realize the 10 MCP UI/UX mockups (`docs/ui/mcp-ui-mockups.html`) as components in the modular webview, wired to the live daemon `/mcp/*` API (Phase 4, already complete). No Python changes.

---

## Context & Architecture Constraints

- **ES modules only.** Each new component is a directory containing `<name>.js` + a co-located `<name>.test.js`. Match the existing pattern exactly: `permissions-list/`, `confirm-card/`, etc.
- **Test harness:** Vitest + happy-dom. Run focused: `npm --prefix ui test -- <file.test.js>`. Full suite: `make ui-test`. Write `.test.js` first for logic-bearing units.
- **Daemon client (`ui/orb/lib/daemon.js`):** All HTTP calls go through `daemon.get(path)` / `daemon.post(path, body)`. All WS events come through `daemon.on(type, fn)` → returns unsubscribe fn. The existing singleton is the only API surface; do not hand-roll `fetch`.
- **Token additions (`ui/orb/styles/tokens.css`):** Add `--net` (orange, `#fb923c` light / `#fb923c` dark) and `--net-bg` (`rgba(251,146,60,.14)` / `rgba(251,146,60,.18)`) for all MCP network-egress styling. Match the `--ok`/`--warn`/`--danger` pattern.
- **Style placement:** MCP-specific styles go into the existing CSS files that already own that surface: `styles/settings.css` for settings/connections tab; `styles/chat.css` for step badges; `styles/orb.css` for the egress ring. Never create new CSS files.
- **Settings tab registration pattern:** `settings-tabs.js` activates `#tab-{name}` panel by id. Adding a tab = add `<button data-tab="connections">` in `settings.html` and a `<section class="panel" id="tab-connections">`. Handle `tab-change` → `"connections"` in `pages/settings.js` to lazy-load the connections-list.
- **Verification gate per task:** `npm --prefix ui test -- <focused>` green, then `make ui-test` full suite green, then `make check` (Python suite unaffected).
- **Conventional Commits; NO Co-Authored-By trailer; stage explicit paths only.**

---

## Reconnect-Churn Tolerance (applies to all live-server UI)

Every CRUD operation (enable/disable, tool override, add/update) triggers a server disconnect → connect cycle on the engine, emitting two `mcp_status` WS events: state flips to `disconnected` then back to `connected`, and `tool_count` transiently becomes 0.

**Design requirement:** The connections list MUST NOT flash a permanent "disconnected" state. Use a 1500 ms debounce on `mcp_status` events that change `state`. During the debounce window, show a brief `"reconnecting…"` sub-label on the affected server card instead of switching the status dot to off. After the debounce, apply the final state. If the final state is still `disconnected`, show the error dot.

Implementation: maintain a `Map<serverId, timeoutId>` in the connections-list component. On each `mcp_status` event, cancel any pending timer for that server, set the card to `"reconnecting…"` label, then arm a 1500 ms timeout that applies the received state.

---

## Daemon API Endpoints (Phase 4 — already live)

| Method | Path | Returns |
|--------|------|---------|
| GET | `/mcp/servers` | `{ok, servers:[{server,label,enabled,egress,auth_type,state,tool_count,secret_present}]}` |
| POST | `/mcp/servers` | descriptor → `{ok, server}` |
| DELETE | `/mcp/servers/{id}` | `{ok}` |
| POST | `/mcp/servers/{id}/enable` | `{ok}` |
| POST | `/mcp/servers/{id}/disable` | `{ok}` |
| POST | `/mcp/servers/{id}/connect` | `{ok}` |
| POST | `/mcp/servers/{id}/test` | `{ok}` |
| GET | `/mcp/servers/{id}/tools` | `[{name,description,risk,network,enabled}]` |
| POST | `/mcp/servers/{id}/tools/{tool}` | body `{risk?,enabled?}` → `{ok}` |
| POST | `/mcp/servers/{id}/auth/start` | `{ok, message}` (Phase-6 stub — UI shows "coming soon") |
| POST | `/secret` | body `{name:"mcp.<id>.token", value}` → `{ok}` |

WS event: `{type:"mcp_status", server, state, tool_count}`.

---

## Global Constraints (verbatim)

- Modular JS (ES modules, the existing style); each new component is a directory with its module + a co-located `*.test.js` (Vitest + happy-dom). Match the existing components' structure exactly.
- TDD where practical: write the `.test.js` first for logic-bearing units (daemon methods, list rendering, badge derivation, confirm-card kind, privacy inventory). Pure-visual bits (orb ring) get a lighter test.
- Reuse `lib/daemon.js` (don't hand-roll fetch); reuse `tokens.css`; follow the mockups' copy/labels.
- Python is UNCHANGED in this phase (no `src/autobot` edits) — the daemon API is already done.
- **Verification gate per task:** `make ui-test` (Vitest) green AND `make check` (Python suite still green, unaffected). The user will visually smoke-test the rendered UI.
- Conventional Commits, **NO `Co-Authored-By`/attribution trailer**; stage explicit paths only (never `git add -A`).
- Branch `feat/mcp-integration`.

---

## Tasks

### Task 1 — Design tokens + `daemon.js` MCP methods

**Deliverable:** `--net`/`--net-bg` token pair added to `tokens.css`; all MCP named wrappers added to `daemon.js`; tests added to `daemon.test.js`.

**Rationale:** Every later task depends on these. Write the test first (TDD gate) — the daemon methods are pure fetch wrappers so they are fully testable before any component exists.

**Files to edit:**
- `ui/orb/styles/tokens.css` — add tokens
- `ui/orb/lib/daemon.js` — add methods
- `ui/orb/lib/daemon.test.js` — add test cases

**Token additions (end of `:root` block and matching dark block):**
```css
/* MCP network-egress */
--net: #e8740e;          /* slightly toned for light mode legibility */
--net-bg: rgba(251,146,60,.14);
```
```css
/* dark override */
--net: #fb923c;
--net-bg: rgba(251,146,60,.18);
```

**Method signatures to add to `daemon.js`:**
```js
mcpServers()                                // GET  /mcp/servers
addMcpServer(descriptor)                    // POST /mcp/servers
removeMcpServer(id)                         // DELETE /mcp/servers/{id}  → post workaround: this.post(`/mcp/servers/${id}/delete`)
enableMcpServer(id)                         // POST /mcp/servers/{id}/enable
disableMcpServer(id)                        // POST /mcp/servers/{id}/disable
connectMcpServer(id)                        // POST /mcp/servers/{id}/connect
testMcpServer(id)                           // POST /mcp/servers/{id}/test
mcpTools(id)                                // GET  /mcp/servers/{id}/tools
setMcpToolOverride(id, tool, patch)         // POST /mcp/servers/{id}/tools/{tool}  body={risk?,enabled?}
mcpAuthStart(id)                            // POST /mcp/servers/{id}/auth/start
mcpSetToken(id, token)                      // POST /secret  body={name:`mcp.${id}.token`, value:token}
```

Note on DELETE: since `daemon.get`/`daemon.post` only cover GET and POST, add a `delete(path)` helper (mirrors `post` but `method:"DELETE"`, no body) and use it for `removeMcpServer`. This helper should also have a test.

**Test cases to add to `daemon.test.js` (new `describe("MCP methods")` block):**
- `mcpServers()` calls `GET /mcp/servers`
- `addMcpServer({server:"s",label:"L"})` calls `POST /mcp/servers` with the descriptor as body
- `removeMcpServer("slack")` calls `DELETE /mcp/servers/slack`
- `enableMcpServer("slack")` calls `POST /mcp/servers/slack/enable`
- `mcpTools("slack")` calls `GET /mcp/servers/slack/tools`
- `setMcpToolOverride("slack","search",{enabled:false})` calls `POST /mcp/servers/slack/tools/search` with body
- `mcpSetToken("slack","tok")` calls `POST /secret` with body `{name:"mcp.slack.token",value:"tok"}`

**Verification:** `npm --prefix ui test -- daemon.test.js`

---

### Task 2 — CSS: MCP component styles

**Deliverable:** All MCP-specific CSS added to the existing surface stylesheets. No new CSS files.

**Rationale:** Write styles before components so tests that check class names and DOM structure can run without visual regressions. Grouping all CSS into one task keeps the diffs reviewable.

**Files to edit:**
- `ui/orb/styles/settings.css` — connections list, connection detail, add-connection wizard, privacy summary
- `ui/orb/styles/chat.css` — step badges (`.srvbadge`, `.egress`)
- `ui/orb/styles/orb.css` — egress ring overlay (`.net-ring`, `.net-conn`)

**CSS to add to `settings.css` (inside `@layer components`):**
```css
/* MCP info banner */
.mcp-banner { display:flex; gap:10px; padding:11px 12px; border-radius:10px; font-size:12.5px; line-height:1.45;
  background:var(--net-bg); border:1px solid rgba(251,146,60,.28); color:var(--net); margin-bottom:14px; }
.mcp-banner.local { background:rgba(40,180,90,.08); border-color:rgba(40,180,90,.25); color:var(--ok); }

/* MCP server card (connections list) */
.srv-card { display:flex; align-items:center; gap:12px; padding:11px 14px;
  border-top:.5px solid var(--line); }
.srv-card:first-child { border-top:none; }
.srv-icon { width:36px; height:36px; border-radius:9px; flex:0 0 auto;
  display:flex; align-items:center; justify-content:center; font-size:18px;
  background:var(--seg-bg); border:.5px solid var(--line); }
.srv-meta { flex:1; min-width:0; }
.srv-name { font-weight:600; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.srv-desc { color:var(--muted); font-size:12px; margin-top:2px; display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.status-dot { width:7px; height:7px; border-radius:50%; display:inline-block; flex:none; }
.status-dot.connected { background:var(--ok); }
.status-dot.disconnected { background:var(--muted); }
.status-dot.auth_needed { background:var(--warn); }
.status-dot.reconnecting { background:var(--warn); }
.add-conn-row { display:flex; justify-content:flex-end; padding:12px 14px 2px; }

/* MCP risk pills */
.pill { display:inline-flex; align-items:center; gap:5px; padding:1px 8px;
  border-radius:999px; font-size:11px; font-weight:600; line-height:1.7; white-space:nowrap; }
.pill.read  { background:rgba(10,132,255,.13); color:var(--accent); }
.pill.write { background:rgba(230,135,14,.14); color:var(--net); }
.pill.danger { background:var(--danger-bg); color:var(--danger); }
.pill.net   { background:var(--net-bg); color:var(--net); }
.pill.local { background:rgba(40,180,90,.12); color:var(--ok); }

/* Add-connection wizard stepper */
.wizard-steps { display:flex; gap:5px; margin-bottom:16px; }
.wizard-step { flex:1; height:4px; border-radius:3px; background:var(--line); }
.wizard-step.done { background:var(--accent); }
.wizard-step.now { background:var(--accent); opacity:.55; }

/* Wizard catalog grid */
.catalog-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px; }
.cat-item { display:flex; align-items:center; gap:9px; padding:10px; border:.5px solid var(--line);
  border-radius:9px; background:var(--panel); cursor:pointer; }
.cat-item:hover { border-color:var(--accent); }
.cat-item.selected { border-color:var(--accent); background:var(--accent-bg); }
.cat-icon { width:28px; height:28px; border-radius:7px; display:flex; align-items:center;
  justify-content:center; font-size:15px; background:var(--seg-bg); flex:none; }
.cat-name { font-weight:600; font-size:12.5px; }
.cat-desc { color:var(--muted); font-size:11px; margin-top:1px; }

/* Wizard radio option */
.opt-item { display:flex; gap:10px; padding:10px; border:.5px solid var(--line);
  border-radius:9px; margin-bottom:8px; background:var(--panel); cursor:pointer; }
.opt-item.selected { border-color:var(--accent); }
.opt-radio { width:15px; height:15px; border-radius:50%; border:2px solid var(--muted); flex:none; margin-top:2px; }
.opt-item.selected .opt-radio { border-color:var(--accent); background:radial-gradient(circle,var(--accent) 38%,transparent 42%); }
.opt-title { font-weight:600; font-size:12.5px; }
.opt-desc  { color:var(--muted); font-size:11.5px; margin-top:2px; }

/* Connection detail tool list */
.tool-row { display:flex; align-items:center; gap:11px; padding:10px 14px;
  border-top:.5px solid var(--line); }
.tool-row:first-child { border-top:none; }
.tool-name { font-weight:600; font-size:13px; }
.tool-desc { color:var(--muted); font-size:12px; margin-top:2px; }
.tool-meta { flex:1; min-width:0; }

/* Privacy summary exit rows */
.exit-row { display:flex; align-items:center; gap:11px; padding:11px 14px;
  border-top:.5px solid var(--line); }
.exit-row:first-child { border-top:none; }
.exit-icon { width:28px; height:28px; border-radius:7px; display:flex; align-items:center;
  justify-content:center; font-size:14px; background:var(--seg-bg); flex:none; }
.exit-meta { flex:1; min-width:0; }
.exit-name { font-weight:600; font-size:13px; }
.exit-desc { color:var(--muted); font-size:12px; margin-top:2px; }

/* Danger zone */
.danger-zone { border-top:.5px solid var(--danger-bg); margin-top:4px; padding-top:12px; display:flex; gap:8px; }
button.btn.danger { background:var(--danger-bg); color:var(--danger); border-color:var(--danger-bg); }
button.btn.ghost-sm { background:transparent; color:var(--muted); border:none; font-size:12px; }
```

**CSS to add to `chat.css` (server badge + egress marker on step trace):**
```css
/* MCP step trace extensions */
.steptrace .srvbadge { display:inline-flex; align-items:center; gap:4px; font-size:10.5px;
  font-weight:600; background:var(--seg-bg); border:.5px solid var(--line);
  padding:1px 6px; border-radius:6px; color:var(--muted); margin-left:6px; }
.steptrace .egress { font-size:10.5px; color:var(--net); margin-left:4px; }
```

**CSS to add to `orb.css` (egress ring — purely additive):**
```css
/* MCP network-egress ring (shown while a network-egress tool is running) */
.net-ring { position:absolute; inset:-14px; border-radius:50%;
  border:2px solid rgba(251,146,60,.55);
  opacity:0; transition:opacity .3s ease; pointer-events:none; }
.net-ring.active { opacity:1; }
.net-conn { position:absolute; right:-8px; bottom:-8px; width:26px; height:26px;
  border-radius:50%; background:var(--panel); border:1.5px solid var(--net);
  display:flex; align-items:center; justify-content:center; font-size:12px;
  opacity:0; transition:opacity .3s ease; pointer-events:none; }
.net-conn.active { opacity:1; }
```

**Verification:** Visual check on smoke-test. No dedicated test for pure-CSS; tested implicitly when component tests check class names.

---

### Task 3 — `connections-list` component (Mockup 1)

**Deliverable:** `ui/orb/components/connections-list/connections-list.js` + `connections-list.test.js`.

**Component:** Custom element `<connections-list>`. Follows the exact `PermissionsList` pattern: `load()` method (called lazily on `tab-change` → `"connections"`), a `#loading` guard, `daemon.mcpServers()` call, renders `.srv-card` rows.

**Behavior:**
- `load()` calls `daemon.mcpServers()`, clears and re-renders server cards. Guards against overlapping loads.
- Each card shows: icon (emoji or `?` placeholder), server name, egress pill (`.pill.net` "↗ sends to …" if `egress` is truthy, else `.pill.local` "● on-device"), status dot (class from `state` field — `connected`/`disconnected`/`auth_needed`), description ("`Connected · N tools · <auth_type>`" or `"Sign-in needed"` when `state === "auth_needed"`), and a `<label class="switch"><input type="checkbox"></label>` toggle wired to `enableMcpServer`/`disableMcpServer`.
- Clicking the card body (not the toggle) dispatches a `"server-select"` CustomEvent with `{detail: server_id}` so the settings page can open the detail view.
- Reconnect-churn tolerance: a `Map<id, timerId>` debounces `mcp_status` events. On receipt: cancel any pending timer; set the affected card's description to `"reconnecting…"` and status dot to class `reconnecting`; arm 1500 ms → apply received `state`/`tool_count`. Subscribe to `mcp_status` in `connectedCallback`; unsubscribe in `disconnectedCallback`.
- An `"+ Add connection"` button at the bottom dispatches `"add-connection"` CustomEvent.

**Interfaces:**
```js
export class ConnectionsList extends HTMLElement {
  #loading = false;
  #offStatus = null;     // WS unsubscribe fn
  #debounce = new Map(); // id -> timeoutId

  connectedCallback()    // subscribe to mcp_status
  disconnectedCallback() // unsubscribe
  async load()           // guard + daemon.mcpServers() + render
  #renderCard(srv)       // returns div.srv-card DOM node
  #applyStatus(id, state, tool_count) // updates a rendered card in place
}
customElements.define("connections-list", ConnectionsList);
```

**Test cases (write first):**
- Renders N server cards from `mcpServers()` response.
- Card shows `.pill.net` when `egress` is truthy.
- Card shows `.pill.local` when `egress` is falsy.
- Status dot has class `connected` when `state === "connected"`.
- Toggle click calls `disableMcpServer(id)` when currently enabled.
- Toggle click calls `enableMcpServer(id)` when currently disabled.
- `mcp_status` WS event applies `"reconnecting…"` immediately, then resolves after debounce.
- `"+ Add connection"` button dispatches `"add-connection"` event.
- `load()` guard prevents concurrent renders.
- Selecting a card body (not toggle) dispatches `"server-select"` with the id.

**Verification:** `npm --prefix ui test -- connections-list.test.js`

---

### Task 4 — Settings page wiring: Connections tab + Privacy tab (Mockups 1, 10)

**Deliverable:** Edits to `ui/orb/settings.html` (new tab buttons + panel markup) and `ui/orb/pages/settings.js` (import, lazy-load, event wiring). Also implements the Privacy tab as a simple function (not a custom element — it's too data-driven to need one), pulling from `daemon.settings()` + `daemon.mcpServers()`.

**Rationale:** No new component file needed for either tab shell or the privacy summary — both are orchestration code that lives in `settings.js` and the HTML template.

**HTML edits (`ui/orb/settings.html`):**
1. Add to `<settings-tabs>`: `<button data-tab="connections">Connections</button>` and `<button data-tab="privacy">Privacy</button>`.
2. Add panels:
   ```html
   <section class="panel" id="tab-connections">
     <connections-list id="connList"></connections-list>
   </section>
   <section class="panel" id="tab-privacy">
     <div id="privacyPanel"></div>
   </section>
   ```

**`settings.js` edits:**
```js
import "../components/connections-list/connections-list.js";
import "../components/add-connection/add-connection.js";
import "../components/connection-detail/connection-detail.js";
```

Add to the `tab-change` listener:
```js
if (e.detail === "connections") $("connList").load();
if (e.detail === "privacy") renderPrivacy($("privacyPanel"));
```

Add `renderPrivacy(el)` function: calls `daemon.settings()` + `daemon.mcpServers()` concurrently, builds the privacy exit rows (web search if `allow_web`, cloud LLM if `llm_provider === "anthropic"`, each enabled network MCP server), and renders them into `el` as `.exit-row` nodes. Includes a "View audit log →" link (fires `daemon.get("/report/file")` or a suitable audit endpoint).

Wire `connList` events:
```js
$("connList").addEventListener("server-select", (e) => openConnectionDetail(e.detail));
$("connList").addEventListener("add-connection", () => openAddConnection());
```

`openConnectionDetail(id)` and `openAddConnection()` instantiate/show the respective components (Tasks 5 and 6) — implemented as functions that manage a modal/overlay container in the settings panel.

**Test:** No dedicated test for `settings.js` orchestration (it is integration glue). The components it wires have their own tests. Visual smoke-test covers the tab switch + lazy-load.

**Verification:** `make ui-test` (full suite, no regressions).

---

### Task 5 — `add-connection` wizard (Mockups 2–5)

**Deliverable:** `ui/orb/components/add-connection/add-connection.js` + `add-connection.test.js`.

**Component:** Module-pattern (not a custom element; created imperatively like `confirm-card`). Exports `showAddConnection(container, {onDone, onCancel})` and `hideAddConnection(container)`.

**4-step wizard state machine:**
- **Step 1 — Source:** Catalog grid (Slack/GitHub/Local Files/Notion/Custom). "Selected" item highlighted. "Custom" is always at bottom spanning full width (matches mockup 2). Continue → step 2.
- **Step 2 — Transport:** For catalog items: pre-fill transport type (HTTP vs stdio) + server URL (read-only). For Custom: show editable radio (HTTP / stdio) → HTTP shows URL field; stdio shows command + args + env fields. Back → step 1. Continue → step 3.
- **Step 3 — Auth:** Radio: "Sign in with OAuth 2.1" (recommended) / "Paste a bot token". Both options always shown. The Keychain banner is always shown (matches mockup 4). Back → step 2. Continue → step 4.
- **Step 4 — Final action:**
  - If "OAuth" selected: show the hand-off explainer (mockup 5 — `↗` banner, disclosure of what leaves the Mac, scoped permissions list). "Open browser" → calls `daemon.mcpAuthStart(id)` → receives the Phase-6 stub → shows "OAuth not yet supported (coming in Phase 6)" inline message. Does NOT close the wizard.
  - If "Token" selected: show a token input field. "Connect" → calls `daemon.addMcpServer(descriptor)` then `daemon.mcpSetToken(id, token)` then `daemon.enableMcpServer(id)` then calls `onDone()`.

**Descriptor shape built across steps:**
```js
{ server: id, label, transport: "http"|"stdio", url?, command?, args?, env?, auth_type: "none"|"token"|"oauth" }
```

**Catalog manifest (static, inline in the module):**
```js
const CATALOG = [
  { id:"slack",   label:"Slack",        icon:"💬", transport:"http",  url:"https://slack.com/api/mcp",         auth:"oauth",  egress:true,  desc:"OAuth / bot token" },
  { id:"github",  label:"GitHub",       icon:"🐙", transport:"http",  url:"https://api.github.com/mcp",        auth:"oauth",  egress:true,  desc:"OAuth" },
  { id:"files",   label:"Local Files",  icon:"📁", transport:"stdio", command:"npx @mcp/server-files",          auth:"none",   egress:false, desc:"on-device" },
  { id:"notion",  label:"Notion",       icon:"🗒️", transport:"http",  url:"https://api.notion.com/v1/mcp",     auth:"oauth",  egress:true,  desc:"OAuth" },
];
```

**Step progress bar:** 4 `.wizard-step` divs; `done` for completed steps, `now` for active.

**Cancel:** calls `onCancel()` from any step.

**Interfaces:**
```js
export function showAddConnection(container, { onDone, onCancel }) → card element
export function hideAddConnection(container)
```

**Test cases (write first):**
- Renders catalog items including "Custom" as the last item.
- Clicking a catalog item selects it (adds `selected` class).
- Continue from step 1 advances to step 2 (step bar reflects it).
- Back from step 2 returns to step 1.
- Selecting Custom in step 1 shows editable command/URL fields in step 2.
- HTTP transport radio shows URL field; stdio shows command+args.
- Selecting "Token" in step 3 and continuing shows token input in step 4.
- Selecting "OAuth" in step 3 and continuing shows the hand-off explainer.
- Submitting token calls `addMcpServer`, `mcpSetToken`, `enableMcpServer` in order, then `onDone`.
- Calling `mcpAuthStart` (OAuth branch) shows "coming soon" message on stub response.
- Cancel dispatches `onCancel`.
- Progress bar shows correct `done`/`now` classes at each step.

**Verification:** `npm --prefix ui test -- add-connection.test.js`

---

### Task 6 — `connection-detail` component (Mockup 6)

**Deliverable:** `ui/orb/components/connection-detail/connection-detail.js` + `connection-detail.test.js`.

**Component:** Module-pattern. Exports `showConnectionDetail(container, serverId, serverMeta)` and `hideConnectionDetail(container)`.

**Layout (matches mockup 6):**
- Header row: server icon + name + status dot + description + "Sign out" ghost button (calls `daemon.mcpAuthStart(id)` — shows "OAuth not yet supported" note for OAuth servers; for token servers, clears the secret via `daemon.mcpSetToken(id, "")` and reloads).
- MCP banner: orange network banner if `egress`, green local banner if not.
- Section label: "Tools · N — toggle off any you don't want Jack to use".
- Tools list: calls `daemon.mcpTools(id)`. Each row: `.tool-name`, `.tool-desc`, a risk pill (editable — clicking cycles read→write→danger and calls `daemon.setMcpToolOverride(id, tool, {risk: next})`), `.switch` enable toggle (calls `daemon.setMcpToolOverride(id, tool, {enabled: bool})`).
- Danger zone: "Remove connection" (calls `daemon.removeMcpServer(id)` then `onClose`) and "Re-sync tools" (calls `daemon.connectMcpServer(id)` then re-loads tool list).

**Risk cycle:** `read → write → danger → read`. Network tools (where `tool.network === true`) are floored at `write` — clicking `read` on a network tool advances to `write`, not `read`.

**Subscribe to `mcp_status`:** When state changes for this server, update the header's status dot/description in place (no full reload needed). Apply the same reconnect-churn debounce (1500 ms).

**Interfaces:**
```js
export function showConnectionDetail(container, serverId, serverMeta, { onClose }) → element
export function hideConnectionDetail(container)
```

**Test cases (write first):**
- Renders tool rows from `mcpTools(id)`.
- Tool toggle calls `setMcpToolOverride(id, tool, {enabled:false})` when toggled off.
- Risk pill click cycles: read → write → danger → read.
- Network tool (`network:true`) risk pill cannot go below write; `read` click yields `write`.
- "Re-sync tools" calls `connectMcpServer(id)` then reloads.
- "Remove connection" calls `removeMcpServer(id)` then `onClose`.
- `mcp_status` event updates the status dot; debounce shows `reconnecting` first.

**Verification:** `npm --prefix ui test -- connection-detail.test.js`

---

### Task 7 — `confirm-card` extension: `kind:"network"` (Mockup 7)

**Deliverable:** Edits to `ui/orb/components/confirm-card/confirm-card.js` and `confirm-card.test.js`.

**Change:** The `showConfirm(log, prompt, kind, options)` signature is unchanged. Extend it to accept an optional 5th argument `meta = {}` with shape `{server?, serverLabel?, egress?}`. When `kind === "network"`, the card:
- Gets an additional CSS class `network` (orange tint via `--net-bg` border, no heading change).
- Renders two extra `.kv` rows after the existing prompt body: "Connection" → `<span class="srvbadge">` with serverLabel; "Data path" → `<span class="egress">↗ text sent to …</span>`.
- The "⚠️ Just checking" heading becomes "Allow network action" for `kind:"network"`.

Keep backward compatibility: existing calls with 4 args or with `kind` != `"network"` are unchanged.

**CSS addition (`settings.css` or `chat.css` — whichever already styles `.confirm`):**
Locate the existing confirm card styles and add:
```css
.confirm.network { border-color:rgba(251,146,60,.35); }
.confirm .kv { display:flex; justify-content:space-between; font-size:12.5px; padding:5px 0; border-bottom:.5px dashed var(--line); }
.confirm .kv:last-of-type { border-bottom:none; }
.confirm .kv .k { color:var(--muted); }
.srvbadge { display:inline-flex; align-items:center; gap:4px; font-size:10.5px; font-weight:600;
  background:var(--seg-bg); border:.5px solid var(--line); padding:1px 6px; border-radius:6px; color:var(--muted); }
.egress { font-size:10.5px; color:var(--net); }
```

**Test cases to add to `confirm-card.test.js`:**
- `kind:"network"` card has class `network`.
- `kind:"network"` card heading is "Allow network action".
- `kind:"network"` with `{serverLabel:"Slack", egress:"text sent to slack.com"}` renders `.srvbadge` with "Slack" and `.egress` with "↗ text sent to slack.com".
- Existing `kind:"danger"` test still passes (no regression).

**Verification:** `npm --prefix ui test -- confirm-card.test.js`

---

### Task 8 — `chat-log` step badge extension (Mockup 8)

**Deliverable:** Edits to `ui/orb/components/chat-log/chat-log.js`, `chat-log.test.js`, and `ui/orb/pages/chat.js`.

**CRITICAL — derive the badge from the tool name, NOT from the step event.** The step
event is deliberately UNCHANGED (no Python change): `m` is `{index, label, tool, status}`
with NO `server`/`egress` fields. The design (`docs/plans/mcp-integration-design.md` §10)
says the UI derives the connection badge + ↗ from the namespaced tool name (`"<id>__<tool>"`)
matched against the cached `mcpServers()` map. So do NOT read `m.server`/`m.egress` — they
do not exist.

**Add a pure helper** `mcpInfoForTool(tool, serverMap)` (export it for unit testing):
- Split `tool` on the FIRST `"__"`. If there's no `"__"`, or the prefix is empty, or the
  prefix is NOT a key in `serverMap` → return `null` (a plain built-in tool, no badge).
- Otherwise return `{ id, label: serverMap[id].label, icon: serverMap[id].icon,
  egress: serverMap[id].egress === "network", shortName: <part after the first "__"> }`.
- Matching the prefix against the known-server-id map disambiguates an MCP tool from a
  built-in whose name might contain `"__"`.

**Change to `renderStep(m)`:** compute `const info = mcpInfoForTool(m.tool, _serverMap)`.
When `info` is non-null:
- Append a `.srvbadge` span: `<span class="srvbadge">{info.icon} {info.label} · {info.shortName}</span>`.
- When `info.egress` is truthy, append `<span class="egress">↗ {info.id}</span>` after it.
When `info` is null, render exactly as today (no badge) — built-in/unknown tools.

**Server map cache:** module-level `let _serverMap = {}` in `chat-log.js`, populated from
`daemon.mcpServers()` → `{[id]: {label, icon, egress}}` (icon: a per-id glyph, default `"🔌"`).
Refresh on new session (reset + re-fetch on `showEmpty()`) and on `mcp_status`. On fetch
failure `_serverMap` stays `{}` → steps render without badges (graceful). Because the map loads
async, the FIRST MCP step in a fresh session may render badge-less until it loads — acceptable;
the running→done re-render shows it.

**Wire in `chat.js`:** load the map when the drawer opens; refresh on `daemon.on("mcp_status", ...)`.

**Test cases to add to `chat-log.test.js`** (test `mcpInfoForTool` directly; seed `_serverMap`):
- `mcpInfoForTool("slack__search_messages", {slack:{label:"Slack",icon:"💬",egress:"network"}})`
  → `{id:"slack", label:"Slack", egress:true, shortName:"search_messages", ...}`.
- `mcpInfoForTool("get_time", {...})` → `null` (no `"__"`, built-in).
- `mcpInfoForTool("unknown__x", {slack:...})` → `null` (prefix not a known server).
- `mcpInfoForTool("files__read", {files:{label:"Files",icon:"📁",egress:"local"}})` →
  `egress:false` (local server: badge yes, ↗ no).
- `renderStep` for a known network MCP tool renders `.srvbadge` AND `.egress`; for a local
  MCP tool renders `.srvbadge` and NO `.egress`; for a built-in renders neither (no regression).

**Verification:** `npm --prefix ui test -- chat-log.test.js`

---

### Task 9 — Orb egress ring (Mockup 9)

**Deliverable:** Edits to `ui/orb/pages/orb.js` and `ui/orb/lib/orb-renderer.js` (or the orb HTML), and a light test.

**Visual design (matches mockup 9):** An orange ring (`.net-ring`) + connector glyph div (`.net-conn` with ↗ text) layered absolutely over the orb canvas. Show when a network-egress tool is running; fade out after the step completes.

**Implementation approach:** Keep `orb-renderer.js` pure (no WS coupling). The ring is a CSS overlay — two `div` elements (`#net-ring`, `#net-conn`) in `index.html` (the orb page), absolutely positioned over the orb stage, with classes toggled by `orb.js`.

**Trigger — derive egress from the tool name (same as Task 8; step events carry NO egress field).**
Reuse the exported `mcpInfoForTool(tool, serverMap)` helper from `chat-log.js` (import it) and a
local cached `_serverMap` (from `daemon.mcpServers()`, refreshed on `mcp_status`). In `orb.js`,
`daemon.on("step", (m) => { const info = mcpInfoForTool(m.tool, _serverMap); ... })`:
- If `info && info.egress && m.status === "running"` → add class `active` to `#net-ring` and `#net-conn`.
- If `m.status === "done" || m.status === "failed"` and active → 800 ms fade-out (remove `active`; CSS `transition:opacity .8s ease`).
- If `info` is null or `info.egress` is false (built-in or local-only tool) → do nothing (no ring).

**Caption:** `<div id="net-cap" class="orbcap">Reaching {info.label}…</div>` below the orb, visible while active. The label comes from `info.label` (derived from the cached server map), never from a step field.

**HTML additions to `ui/orb/index.html`** (inside `.stage`, after the canvases):
```html
<div id="net-ring" class="net-ring" aria-hidden="true"></div>
<div id="net-conn" class="net-conn" aria-hidden="true">↗</div>
<div id="net-cap" class="orbcap" aria-hidden="true"></div>
```

**Test (lighter — no WebGL):** A new `ui/orb/pages/orb-egress.test.js` (or colocated) that mounts only the egress DOM elements (not the orb renderer), stubs `daemon`, seeds the server map (e.g. `{slack:{label:"Slack",egress:"network"}, files:{label:"Files",egress:"local"}}`), and verifies:
- `step` with `tool:"slack__send", status:"running"` (network server) adds class `active` to `#net-ring`/`#net-conn` and sets the caption to "Reaching Slack…".
- a following `step` with the same tool and `status:"done"` removes class `active`.
- `step` with `tool:"files__read", status:"running"` (LOCAL server) does NOT activate the ring.
- `step` with `tool:"get_time", status:"running"` (built-in, no `"__"`) does NOT touch the ring.

**Verification:** `npm --prefix ui test -- orb-egress.test.js`

---

## Task Execution Order

```
Task 1 (tokens + daemon methods)
  → Task 2 (CSS)
    → Task 3 (connections-list)          Task 7 (confirm-card network kind)
      → Task 4 (settings wiring)         Task 8 (chat-log step badge)
        → Task 5 (add-connection wizard) Task 9 (orb egress ring)
        → Task 6 (connection-detail)
```

Tasks 7, 8, and 9 can run in parallel with tasks 4–6 after Task 2 (CSS) is done. Tasks 3–6 are sequential (each depends on the previous).

---

## File Map

| Task | Creates | Edits |
|------|---------|-------|
| 1 | — | `ui/orb/styles/tokens.css`, `ui/orb/lib/daemon.js`, `ui/orb/lib/daemon.test.js` |
| 2 | — | `ui/orb/styles/settings.css`, `ui/orb/styles/chat.css`, `ui/orb/styles/orb.css` |
| 3 | `ui/orb/components/connections-list/connections-list.js`, `…test.js` | — |
| 4 | — | `ui/orb/settings.html`, `ui/orb/pages/settings.js` |
| 5 | `ui/orb/components/add-connection/add-connection.js`, `…test.js` | — |
| 6 | `ui/orb/components/connection-detail/connection-detail.js`, `…test.js` | — |
| 7 | — | `ui/orb/components/confirm-card/confirm-card.js`, `…test.js` |
| 8 | — | `ui/orb/components/chat-log/chat-log.js`, `…test.js`, `ui/orb/pages/chat.js` |
| 9 | `ui/orb/pages/orb-egress.test.js` | `ui/orb/pages/orb.js`, `ui/orb/index.html` |

**Total new files:** 7 (3 component pairs + 1 test file).
**Total edits:** 11 existing files.
**Python files changed:** 0.

---

## Verification Checklist (per task, before marking done)

1. Focused test: `npm --prefix ui test -- <task-file>.test.js` — all green.
2. Full suite: `make ui-test` — no regressions.
3. Python suite: `make check` — still green (no Python edits in this phase).
4. Visual smoke-test: user opens Settings → Connections tab, confirms list renders, toggling enable/disable works, "Add connection" wizard opens, detail view opens, chat step badges appear on MCP tool calls, orb ring appears during egress call, Privacy tab lists active exits.

---

## Assumptions & Open Questions

1. **`DELETE` method:** The current `daemon.js` has only `get`/`post`. A `delete(path)` helper is added in Task 1. If the daemon API uses `POST /mcp/servers/{id}/delete` instead of HTTP DELETE, change `removeMcpServer` to use `post` — check the Phase 4 route definitions before implementing.

2. **Server icon:** The daemon `/mcp/servers` response includes `label` but not an `icon` emoji. The connections-list will use a simple `label[0].toUpperCase()` initials fallback if no icon is provided. If Phase 4 adds an `icon` field, use it directly. Confirm with Phase 4 schema before Task 3.

3. **Step events carry NO `server`/`egress` fields (resolved):** Per design §10 the StepEvent is deliberately unchanged. Tasks 8 and 9 DERIVE the server + egress from the namespaced tool name (`"<id>__<tool>"`) matched against the cached `mcpServers()` map (the shared `mcpInfoForTool` helper). No Python/StepEvent change is needed or made.

4. **Settings HTML location:** The plan assumes the settings HTML file is at `ui/orb/settings.html`. Confirm the exact path (it may be `ui/orb/pages/settings.html` or served from a different location) before Task 4.

5. **`chat.css`/`orb.css` paths:** These are referenced above but not confirmed to exist as separate files — they may be `styles/chat.css` and `styles/orb.css`. Check `ui/orb/styles/` before Task 2.

6. **Orb `index.html`:** Task 9 edits `ui/orb/index.html` to add ring elements. Confirm this is the orb page template before implementing.

7. **`mcp_status` subscription lifetime:** `connections-list` subscribes in `connectedCallback` and unsubscribes in `disconnectedCallback`. Settings keeps the element permanently mounted, so the subscription is live for the entire settings window lifetime — this is correct and intentional.

---

## Self-Review

- **Completeness:** All 10 mockups are covered. Mockup 5 (OAuth hand-off) is handled inside the wizard (Task 5, step 4, OAuth branch). Mockup 10 (Privacy) is handled inside the settings wiring (Task 4).
- **Reconnect churn:** Explicitly addressed in Task 3 and Task 6 with a 1500 ms debounce pattern.
- **Backward compatibility:** Every change to existing components (`confirm-card`, `chat-log`) guards the no-`server`/no-`egress` case so today's tests still pass.
- **No Python changes:** Confirmed — all 9 tasks are JS/CSS only.
- **TDD coverage:** Tasks 1, 3, 5, 6, 7, 8, 9 have explicit test-first requirements. Tasks 2 and 4 are CSS/orchestration with implicit coverage.
- **Scope realism:** 9 tasks, ~7–9 implementation sessions. The wizard (Task 5) is the most complex; connection-detail (Task 6) is second. Both have comprehensive test specs to keep implementation honest.
