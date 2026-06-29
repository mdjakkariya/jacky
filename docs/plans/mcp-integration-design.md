# MCP Integration — Design (Jack / autobot)

> Status: **agreed design**. Validated against the codebase and against the official
> `mcp` Python SDK (verified at **v1.28.1**) and the current MCP spec (authorization
> revision **2025-11-25**). Tracking and status live in **GitHub Issues + Project #1**
> per `CLAUDE.md` — this is a design reference, not a tracking doc. Each
> implementation phase below becomes an issue; every PR links it with `Closes #NN`.

## 1. The one-sentence idea

An MCP server is just **another source of `ToolSpec`s** for the existing
`ToolRegistry`. Once an MCP tool is registered, it flows model → executor →
`PermissionGate` → `ToolRegistry.dispatch` exactly like a built-in tool. The
LLM, the orchestrator, and the state machine **do not change at all**. We add a
new `mcp/` subpackage that connects to servers, adapts their tools into
`ToolSpec`s, and wires into `app.py::build()` — plus a handful of small, required
changes to existing seams (listed in §4).

This is why the existing architecture is a good fit: the protocol/registry/gate
seam was designed for exactly this kind of swap, and the gate already gives us
risk classification, confirmation, and an audit trail for free.

## 2. Why this needs care: the privacy constraint

`CLAUDE.md` constraint #1 is **on-device only**, with two disclosed, opt-in,
off-by-default exceptions (the `web_search` query, and the optional Anthropic
cloud LLM). A remote MCP server such as Slack **sends user data off the
machine**, so MCP is treated as a **third disclosed exception** — and because it
is a whole *class* of exits rather than a single feature, it gets stronger
controls than either existing exception.

The design draws a hard line between two kinds of server:

- **Local servers** (stdio subprocess, e.g. a filesystem or sqlite MCP) that
  never touch the network — still "on-device", gated like any built-in tool, no
  new disclosure.
- **Network-egress servers** (remote HTTP, or a local stdio server that itself
  calls out, like Slack) — the disclosed exception. Off by default, individually
  enabled, labelled in the UI with a "sends data to `<host>`" badge, and every
  call recorded in the audit log.

A single **master flag `allow_mcp` (default `False`)** gates the whole subsystem,
mirroring `allow_web`.

## 3. Three corrections that shape the design

These are where the first-draft proposal diverged from what the code and the
verified SDK actually require. They drive the rest of the document.

### 3.1 The async bridge is a per-server worker, not per-call

The MCP SDK is `asyncio`/anyio with structured concurrency. A `ClientSession`'s
transport context managers **and all of its calls must run on one task, on one
event loop** — you cannot call `session.call_tool` from a task other than the one
holding the `async with` open (anyio raises a cancel-scope error, and for stdio
the subprocess would be re-spawned per call). So the bridge is **one long-lived
worker coroutine per server**:

1. The worker enters the transport + `ClientSession` context managers,
   `initialize()`s, checks capabilities, `list_tools()`, adapts + registers
   `ToolSpec`s, then loops awaiting commands from an `asyncio.Queue`.
2. Each adapted (synchronous) `ToolSpec.handler` enqueues `(CALL, tool, args,
   future)` via `loop.call_soon_threadsafe` and blocks on
   `future.result(timeout)`.
3. The worker also serves `RESYNC` (re-`list_tools` after `tools/list_changed`)
   and `SHUTDOWN` (exit the context managers, which terminates and reaps a stdio
   subprocess).

Timeouts and cancellation surface as failed `ToolResult`s — never exceptions —
preserving the "tools never raise out of dispatch" rule. A disconnected server
returns a failed `ToolResult` ("server unavailable") rather than crashing the
turn.

### 3.2 OAuth is ~90% the SDK's job — we configure, not build

`mcp.client.auth.OAuthClientProvider` (a subclass of `httpx.Auth`, passed to the
Streamable-HTTP client via `auth=`) already performs, automatically:

- protected-resource-metadata discovery (RFC 9728) triggered by `WWW-Authenticate`
  on a 401,
- authorization-server metadata / OIDC discovery (RFC 8414),
- **PKCE S256** (RFC 7636),
- dynamic client registration (RFC 7591) — **now deprecated** in the spec; prefer a
  Client ID Metadata Document (`client_metadata_url`) or pre-registration, with DCR
  as fallback,
- resource indicators (RFC 8707) and issuer validation (RFC 9207),
- token refresh, `403 insufficient_scope` step-up, and `Authorization: Bearer`
  injection.

**We implement only three small pieces:** a `TokenStorage` backed by the Keychain;
a `redirect_handler` that opens the system browser (validate the URL scheme is
`http`/`https`, never via a shell); and a `callback_handler` that stands up a
temporary `127.0.0.1` loopback listener and returns `(code, state)`. We **pin
`mcp>=1.28,<2`** — v2 is alpha and changes the `callback_handler` return type.

### 3.3 The egress→confirm rule has one more clause than "OR network"

The gate today confirms only at `Risk.DESTRUCTIVE`+. We add a first-class
`network: bool` to `ToolSpec`. But confirming *every* network tool would pop a card
on every Slack search, contradicting the mockups (reads = ↗ badge only; sends = ↗
badge + card). The faithful rule:

```
confirm  iff  risk >= DESTRUCTIVE  OR  (spec.network AND risk >= WRITE)
```

- network `READ_ONLY` → ↗ badge + audit egress, **no card**;
- network `WRITE` → ↗ badge + **card** (`kind="network"`, the orange "data path"
  styling);
- anything `DESTRUCTIVE` → card, as today.

`network` drives the ↗ badge + audit egress note for *all* network tools; the
*card* is gated by the clause above.

## 4. Where the code slots in (integration seams)

| Concern | Existing seam | What MCP adds |
|---|---|---|
| Tool shape | `ToolSpec` (`tools/registry.py`) | MCP tools adapted into `ToolSpec`; **new field `network: bool = False`** |
| Tool catalog | `ToolRegistry.register()` / `.schemas()` | **new `unregister(name)` + idempotent re-register** for live resync |
| Execution | `PermissionGate.execute()` | **confirm rule gains the §3.3 clause**; `kind="network"` for egress writes |
| Risk / confirm / audit | `Risk` enum, gate, `AuditLog` | risk mapping (§6); server id already captured via the namespaced `tool` column |
| Confirm card | `ConfirmEvent` / `Confirmer` (`kind`) | new `kind="network"` (orange data-path card) |
| LLM tool schemas | `registry.schemas()` (OpenAI format) | unchanged — adapter emits the same format |
| Config | `Settings` + `settings.json` | **new `allow_mcp` flag** + `mcp/servers.json` descriptor |
| Secrets | Keychain via `autobot.secrets` | tokens under `mcp.<id>.*`; **daemon `_SECRET_NAMES` allowlist must accept the `mcp.*` namespace** |
| Composition | `app.py::build()` | one `if settings.allow_mcp:` block + a new `on_mcp_event` sink |
| Daemon wiring | `daemon/runner.py` → `run_daemon` → `create_app` | manager handle threaded through; `/mcp/*` handlers call it via `asyncio.to_thread` |
| Hot reload | `ReloadableLanguageModel` / `ReloadableSTT` | reloadable `McpManager` (enable/disable/edit without restart) |
| UI ↔ engine | daemon HTTP + WS events | `/mcp/*` endpoints + `mcp_status` / `mcp_oauth` events; step badge **derived in UI** (no `StepEvent` change) |

The LLM clients, the orchestrator, and the state machine are **untouched**.

### New subpackage `src/autobot/mcp/`

- `adapter.py` — **pure functions, unit-tested, no runtime**:
  - `params_from_input_schema(inputSchema) -> ToolSpec.parameters`
  - `result_to_text(CallToolResult) -> (str, bool)` — join text blocks; placeholders
    for image/audio/resource blocks; `isError` → failed.
  - `risk_for(server_cfg, tool) -> Risk` — the §6 policy (annotations are advisory).
  - `fingerprint(tool) -> str` — `sha256(name, description, inputSchema, annotations)`.
- `config.py` — `McpServerConfig` dataclass + load/save of `~/.autobot/mcp/servers.json`
  (descriptor in §5; **config only, never secrets**).
- `session.py` — `McpServerWorker`: the per-server worker coroutine (§3.1). **Lazy-imports
  `mcp`.** Owns connect → `initialize` → capability check → `list_tools` →
  adapt+register; serves `CALL` / `RESYNC` / `SHUTDOWN`; wires `message_handler` so
  `tools/list_changed` pushes `RESYNC`.
- `manager.py` — `McpManager`: owns the dedicated event-loop thread (`run_forever`), all
  workers, the registry handle, the fingerprint store, and the `on_event` sink. Exposes
  a **synchronous** API the daemon calls (list / enable / disable / connect / test /
  auth-start / set-tool-risk). `connect_enabled()` on startup.
- `auth.py` — `KeychainTokenStorage(TokenStorage)`, `open_browser` redirect handler,
  `LoopbackCallbackServer` callback handler, and the static-token header/env builders.

## 5. Standardization: adding an MCP is config, not code

Adding Slack, or any future server, is editing JSON (or using the UI), never touching
Python. `~/.autobot/mcp/servers.json` (config only — **never secrets**):

```jsonc
{
  "servers": {
    "slack": {
      "label": "Slack",
      "transport": "stdio",                 // "stdio" | "http"
      "command": "npx",                      // stdio only
      "args": ["-y", "@modelcontextprotocol/server-slack"],
      "env": { "SLACK_TEAM_ID": "T0123" },   // non-secret env only
      "url": null,                           // http only (Streamable HTTP)
      "auth": { "type": "token" },           // "none" | "token" | "oauth"
      "token_env": "SLACK_BOT_TOKEN",        // stdio: env var the token is injected as
      "secret_ref": "mcp.slack.token",       // Keychain account name, not the value
      "enabled": false,                      // off by default
      "egress": "network",                   // "local" | "network"
      "default_risk": "write",               // floor for this server's tools
      "tool_allow": ["slack_*"],             // allow/deny globs to limit schema bloat
      "tool_risk_overrides": { "slack_send_message": "write" }
    }
  }
}
```

This mirrors the repo's split: **`settings.json` holds config; the Keychain holds
secrets** (`autobot.secrets`, accounts `mcp.slack.token`, `mcp.slack.oauth`,
`mcp.slack.client`). No new privacy model — the same one.

### Standard mechanics that apply to *every* server

- **Tool namespacing.** Register as `<id>__<tool>` (e.g. `slack__send_message`) to avoid
  collisions, staying within the LLM tool-name charset (`[A-Za-z0-9_-]{1,64}`). The id is
  stripped before the SDK call. The id is what the UI parses to derive the step badge.
- **Discovery.** `initialize` → read negotiated protocol version + capabilities →
  `tools/list` → adapt + register each tool. Subscribe via the SDK's single
  `message_handler` to `notifications/tools/list_changed` to re-sync live (must be wired
  explicitly, or the notification is silently dropped).
- **Schema budget.** Per-server `tool_allow`/deny globs and enabled-only advertising keep
  the tool schema small for the local `qwen3:8b` context; cache `tools/list` between
  reconnects.

## 6. Risk & safety policy

- **Annotations are display-only.** The SDK's own `ToolAnnotations` docstring says clients
  must never make tool-use decisions from server-supplied hints. Default mapping (all
  user-overridable per tool, persisted as `tool_risk_overrides`):
  `readOnlyHint → READ_ONLY`; otherwise `WRITE`; `destructiveHint` / delete-shaped →
  `DESTRUCTIVE`.
- **Network server** (`egress: "network"`): every tool gets `network=True` → ↗ badge +
  audit egress note; writes additionally confirm per §3.3. **Local stdio**
  (`egress: "local"`): `network=False`, gated exactly like built-in tools, no ↗.
- **Tool poisoning / rug-pull.** The manager pins approved fingerprints. On resync, a
  changed fingerprint marks the tool *unapproved* (the gate refuses → failed
  `ToolResult`) and emits an `mcp` event so the UI shows a "tool changed — review" diff
  for re-consent.
- **Local-server spawn consent.** Before the first spawn, show the exact, untruncated
  `command + args` and require explicit approval (via the existing confirmer; dovetails
  with the `Sandbox` path-jail). Warn on `sudo` / destructive shapes.
- **Tool results are untrusted content** that re-enters the LLM — kept clearly delimited
  as tool output, with any destructive follow-on still gated.

## 7. Authentication: one flow shape for any server

Three auth types, selected by `auth.type`:

1. **`none`** — local stdio server, no credentials.
2. **`token`** — static bot/API token, stored in Keychain (`mcp.<id>.token`). For **stdio**
   it is injected as an env var (`StdioServerParameters(env={token_env: value, ...})` — the
   spec's sanctioned path for stdio). For **HTTP** it is sent as
   `headers={"Authorization": f"Bearer <token>"}`. Entered once in the UI, never written to
   `servers.json`.
3. **`oauth`** — the SDK's `OAuthClientProvider` does the full OAuth 2.1 + PKCE flow (§3.2).
   We supply:
   - `KeychainTokenStorage(id)` — persists the token bundle (`mcp.<id>.oauth`) and client
     info (`mcp.<id>.client`) as JSON blobs in the Keychain.
   - `redirect_handler(url)` — opens the system browser (scheme-validated, no shell).
   - `callback_handler()` — a temporary `127.0.0.1:<ephemeral>/callback` listener that
     returns `(code, state)`; the registered `redirect_uri` is the loopback URL.

   Wiring: `streamablehttp_client(url, auth=OAuthClientProvider(server_url, OAuthClientMetadata(
   client_name="Jack", redirect_uris=["http://127.0.0.1:<port>/callback"],
   grant_types=["authorization_code", "refresh_token"]), storage=..., redirect_handler=...,
   callback_handler=...))`.

Security baked in by the SDK: PKCE S256, exact redirect-URI match, `state` + issuer
validation, resource-indicator audience binding, per-server token binding, least-privilege
scopes (rely on step-up).

## 8. Production-readiness checklist

**Lifecycle & resilience.** Connect on enable; lazy spawn; graceful shutdown that unwinds
the worker's context managers (reaping stdio subprocesses) with a bounded timeout;
restart-on-crash with exponential backoff; live re-sync on `tools/list_changed`; per-call
timeouts and cancellation. A disconnected server marks its tools unavailable (failed
`ToolResult`) rather than crashing the turn.

**Security & trust boundary.** Tokens only in Keychain. Server-declared risk hints not
trusted (§6). Tool results untrusted. Fingerprint + re-consent on change. Loopback-only
OAuth callback; the daemon is already loopback-only.

**Observability.** New `[mcp]` logger tag with seam events (connect, disconnect, tool
register/unregister, call latency, oauth stage) — no per-token noise. The audit log already
captures every gated call; the namespaced `<id>__tool` name records the server in the
existing `tool` column (the `detail` may be enriched, but no schema change is required).

**Performance / context.** Allow/deny globs + enabled-only advertising keep the tool schema
small for the local model; cache `tools/list` between reconnects.

**Hot reload.** A reloadable `McpManager` so enabling/disabling a server or editing config
applies without a restart.

**Versioning.** Read and record the negotiated protocol version at `initialize`; refuse a
server below a configured minimum if needed. Pin `mcp>=1.28,<2`.

## 9. Wiring (`app.py::build()` + `daemon/runner.py`)

`build()` gains one block and one new sink parameter (`on_mcp_event`, wired in `runner.py`
to `bus.publish_mcp_*`, consistent with the existing `on_step` / `on_choices` sinks):

```python
if settings.allow_mcp:
    mcp = McpManager(
        config=load_mcp_config(),     # ~/.autobot/mcp/servers.json
        registry=registry,            # registers ToolSpecs here
        secrets=secrets,              # Keychain access
        on_event=on_mcp_event,        # mcp_status / mcp_oauth WS events via the bus
    )
    mcp.connect_enabled()             # async, on the manager's bg loop
    orchestrator.mcp = mcp            # exposed so the daemon's /mcp/* handlers can reach it
```

`runner.py` passes the manager to `run_daemon` → `create_app`; the `/mcp/*` handlers call
its synchronous API via `asyncio.to_thread` (the same pattern as `on_action`). The gate,
both LLM clients, the orchestrator, and the state machine are untouched.

## 10. Daemon API additions

`GET /mcp/servers` (list + status), `POST /mcp/servers` (add/update),
`DELETE /mcp/servers/{id}`, `POST /mcp/servers/{id}/enable|disable`,
`POST /mcp/servers/{id}/connect|test`, `POST /mcp/servers/{id}/auth/start`
(opens the OAuth flow), `GET /mcp/servers/{id}/tools`,
`POST /mcp/servers/{id}/tools/{tool}` (set risk override / enable).

New WS events: `mcp_status {server, state, tool_count}`, `mcp_oauth {server, stage}`, plus
tool-registration changes. **The `StepEvent` does not change** — the chat drawer derives the
connection badge and ↗ from the `<id>__` tool-name prefix plus the cached `/mcp/servers` map.
The daemon's `_SECRET_NAMES` validation is extended to accept the `mcp.*` namespace so
`/secret` (or a dedicated auth endpoint) can store `mcp.<id>.*` tokens.

## 11. Slack as the first integration (concrete)

- **Catalog entry** with two connection options: the **remote Slack-hosted server**
  (OAuth 2.1) or a **local stdio server** (`npx`, bot token in Keychain, `SLACK_TEAM_ID`
  in env).
- **Tools** (per Slack's MCP): search channels, read channel/thread, read user profile, send
  message, schedule message, manage canvases, search users.
- **Risk mapping**: search/read → `READ_ONLY` **with a network-egress badge**;
  `send_message` / `schedule_message` / canvas writes → `WRITE` + confirm (`kind="network"`).
- **Auth**: bot token (`mcp.slack.token`, injected as `SLACK_BOT_TOKEN`) or OAuth 2.1 for the
  hosted server.
- **Disclosure**: labelled "Sends messages and search text to Slack" and listed in the
  Privacy summary alongside web search and the cloud LLM.

## 12. UI surfaces (per the mockups in `docs/ui/mcp-ui-mockups.html`)

Additions to the existing shell, no new app:

- `ui/orb/settings.html` — new **Connections** tab (server list), **add-connection** wizard
  (catalog / custom → transport → auth → OAuth hand-off explainer), **connection detail**
  (tools, per-tool risk, danger zone), and the **Privacy summary** (every off-device exit).
- `ui/orb/chat.html` — MCP **step badges** (server + ↗, derived from the tool name) and the
  **network confirm card** (`kind="network"`).
- `ui/orb/index.html` — a subtle **orb egress ring** shown only while a network-egress call
  runs.

## 13. Phasing (each phase = one GitHub issue / reviewable PR)

1. **Pure core** — `adapter.py` + `config.py` + `ToolSpec.network` + registry
   `unregister`/idempotent-replace + `Settings.allow_mcp` + the `mcp` extra. Unit tests
   only; no runtime, no UI.
2. **Async bridge** — `session.py` worker + `manager.py` (loop thread, connect/list/register,
   queue/`CALL`, `tools/list_changed` resync, shutdown + subprocess reaping). Local stdio
   `auth: none`. `[mcp]` logging. `app.py` wiring. Gate egress rule (§3.3) + `kind="network"`.
3. **Token auth** — `auth.py` Keychain storage + token injection (env/header); `mcp.*` secret
   allowlist. **Slack via bot token, end-to-end.**
4. **Daemon `/mcp/*`** endpoints + `mcp_status` / `mcp_oauth` events.
5. **UI** — Connections tab, add-connection wizard, connection detail, network confirm card,
   chat-drawer badges, orb ring, Privacy summary.
6. **OAuth** — `auth.py` redirect + loopback callback handlers + `OAuthClientProvider` wiring
   (HTTP servers); tool-definition fingerprinting and re-consent-on-change UI.

## 14. Testing

Per the repo pattern, pure logic is unit-tested without a live server or the SDK:

- `adapter.py`: schema mapping, result-block flattening (text / image / error / resource),
  risk policy (annotations + overrides + network floor), fingerprinting.
- `config.py`: `servers.json` load/save, glob allow/deny, defaults.
- gate: the §3.3 confirm rule (network read = no card; network write = card; destructive =
  card) and `kind="network"` plumbing.
- registry: `unregister` + idempotent re-register.
- The async bridge (`manager`/`session`) is integration-tested against a tiny in-repo stdio
  echo MCP server, kept out of the fast unit suite.

## Phase 6 — OAuth 2.1, HTTP Transport, Rug-Pull & Spawn Consent

### What was built

| Deliverable | File(s) |
|---|---|
| `KeychainTokenStorage` (async `TokenStorage`) | `src/autobot/mcp/auth.py` |
| `open_browser` (scheme-validated, no shell) | `src/autobot/mcp/auth.py` |
| `LoopbackCallbackServer` (`127.0.0.1:0`, 120 s timeout) | `src/autobot/mcp/auth.py` |
| HTTP transport branch (OAuth 2.1 / token / unauthenticated) | `src/autobot/mcp/session.py` |
| Fingerprint rug-pull detection + `mcp_tool_changed` event | `src/autobot/mcp/session.py` + `src/autobot/mcp/approvals.py` |
| Local-spawn consent (via `Confirmer`) | `src/autobot/mcp/session.py` + `src/autobot/mcp/approvals.py` |
| `approved.json` persistence (mode 0600) | `src/autobot/mcp/approvals.py` |
| `POST /mcp/servers/{id}/auth/start` endpoint (wired) | `src/autobot/daemon/server.py` |

### OAuth setup (`servers.json`)

To add a remote MCP server that uses OAuth 2.1, set `"auth": {"type": "oauth"}` — the
config token is `"oauth"` (the protocol is OAuth 2.1, but the literal config value is `oauth`):

```json
{
  "servers": {
    "github": {
      "label": "GitHub MCP",
      "transport": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "auth": {"type": "oauth"},
      "egress": "network",
      "enabled": false,
      "default_risk": "write"
    }
  }
}
```

Then:

1. Enable the server: `POST /mcp/servers/github/enable`
2. Start OAuth: `POST /mcp/servers/github/auth/start`
3. Jack opens your browser → complete the login → the callback is captured on
   `http://127.0.0.1:<ephemeral-port>/callback` → tokens stored in Keychain as
   `mcp.github.oauth` (token bundle) and `mcp.github.client` (DCR client info).
4. Watch `mcp_oauth` WS events for the three stages:
   `browser_open` → `waiting_callback` → `callback_received`
   followed by an `mcp_status` event with `state=connected`.

#### `auth/start` endpoint behaviour

`POST /mcp/servers/{id}/auth/start` calls `McpManager.start_oauth(id)`, which:

- Returns `{"ok": false, "error": "..."}` if the server's transport is not `http` or
  `auth.type` is not `"oauth"`.
- Disconnects any existing worker for that server, then reconnects — which triggers the
  browser flow via `OAuthClientProvider`.
- Returns `{"ok": true, "started": true}` once the reconnect has been initiated
  (the browser may still be waiting; monitor `mcp_oauth` events for progress).

### HTTP transport: three auth branches

`session.py` opens HTTP connections via `streamablehttp_client` (3-tuple unpacked as
`read, write, _`). Auth is selected by `auth.type`:

| `auth.type` | Behaviour |
|---|---|
| `"oauth"` | `OAuthClientProvider` (SDK) performs discovery, PKCE S256, DCR/refresh, and `Authorization: Bearer` injection automatically |
| `"token"` | Static bearer injected as `Authorization: Bearer <token>` header |
| anything else / omitted | Unauthenticated connection |

### Fingerprint rug-pull detection (R3 — actual behavior)

On every `list_tools` (connect + `tools/list_changed` resync) the worker computes a
`sha256` fingerprint over each tool's `(name, description, inputSchema, annotations)`.

**New tools on first connect** are auto-baselined: their fingerprints are written to
`~/.autobot/mcp/approved.json` and the tools are registered with the `ToolRegistry`.

**Changed tools** (fingerprint in `approved.json` no longer matches) are handled as
follows:

- The tool is **NOT registered** with the `ToolRegistry` — it is simply absent.
  There is no special "re-consent" gate message; the registry returns its normal
  "unknown tool" response if the model tries to call it.
- The worker emits a WS event: `{"type": "mcp_tool_changed", "server": "<id>",
  "tools": ["<namespaced-name>", ...]}`.
- The tool appears in the worker's `all_tools()` snapshot with
  `"pending_reconsent": true`, so the UI can surface a "tool changed — review" indicator.

**Re-approval**: delete the tool's entry from `~/.autobot/mcp/approved.json` and
reconnect. The reconnect calls `list_tools` again, sees the new fingerprint, baselines
it, and registers the tool.

Future: a `POST /mcp/servers/{id}/tools/{tool}/reconsent` endpoint can automate this
without a manual file edit.

### Spawn consent

On the first `stdio` connect for an un-approved `(command, args)` pair:

1. Jack prompts via the `Confirmer` (kind `"write"`) with the exact, untruncated command
   and args: *"Allow Jack to launch this process? npx -y @mcp/fs /path"*.
2. The confirmation runs **off the event loop** via `run_in_executor` so it does not
   block the async worker.
3. **Approval**: written to `~/.autobot/mcp/approved.json` (`spawn_approvals` section,
   keyed by `(command, args)`, with an `approved_at` timestamp). Not re-asked unless
   the command or args change.
4. **Denial**: worker sets server state to `"denied"` and does **not** enter the stdio
   context manager — the process is never spawned.

The confirmer is wired in `app.py::build()` via `McpManager.set_confirmer(...)` **before**
`connect_enabled()` is called.

### `approved.json` persistence

`~/.autobot/mcp/approved.json` (mode 0600) is written and read by pure stdlib — no SDK
dependency, no tokens. Schema:

```json
{
  "fingerprints": {
    "<server-id>": {
      "<server-id>__<tool-name>": "<sha256-hex>"
    }
  },
  "spawn_approvals": {
    "<command> <args-joined>": {
      "command": "npx",
      "args": ["-y", "@mcp/fs", "/path"],
      "approved_at": "2026-06-29T10:00:00Z"
    }
  }
}
```

The fingerprints are hashes of public tool schemas (not sensitive). Spawn records
contain `command + args` (which may include local paths). The file is mode 0600.

### Manual smoke-test (live OAuth) — user steps

Requirements: a real remote MCP server that supports OAuth 2.1 (e.g. GitHub Copilot MCP,
Slack hosted MCP, or a local test authorisation server such as `oauth2-proxy` / `dex`).
These steps are performed **by the user** — they require a real OAuth server and a browser.

```bash
# Start daemon
make run &

# Add server via curl (daemon default port 8765)
curl -s -X POST http://127.0.0.1:8765/mcp/servers \
  -H 'Content-Type: application/json' \
  -d '{"id":"github","label":"GitHub MCP","transport":"http",
       "url":"https://api.githubcopilot.com/mcp/",
       "auth":{"type":"oauth"},"egress":"network","enabled":false}'

# Enable
curl -s -X POST http://127.0.0.1:8765/mcp/servers/github/enable

# Start auth flow (browser will open)
curl -s -X POST http://127.0.0.1:8765/mcp/servers/github/auth/start

# Observe: browser opens, complete login, terminal shows:
#   [mcp] mcp_oauth stage=callback_received server=github
#   [mcp] mcp connected server=github transport=http tools=N

# Verify tools registered
curl -s http://127.0.0.1:8765/mcp/servers/github/tools | python3 -m json.tool
```

### Test coverage

Phase 6 unit coverage lives in:

- `tests/unit/test_mcp_auth.py` — 25 tests covering `KeychainTokenStorage`,
  `LoopbackCallbackServer` (happy path + timeout), and `open_browser` (scheme validation).
- `tests/unit/test_mcp_approvals.py` — fingerprint read/write, spawn-approval persistence.
- `tests/unit/test_mcp_session.py` — HTTP transport branch selection, rug-pull detection,
  spawn consent logic.
- `tests/unit/test_daemon_server.py` — `auth/start` endpoint validation and routing.

### Self-review checklist

- [x] `make check` green (ruff + ruff-format + mypy strict + pytest)
- [x] No token value appears in any log line
- [x] `approved.json` mode 0600
- [x] `open_browser` rejects `file://`, `custom://`, bare strings without `http`/`https` scheme
- [x] `LoopbackCallbackServer` binds to `127.0.0.1`, not `0.0.0.0`
- [x] Stdio path byte-for-byte unchanged (existing `test_mcp_session.py` suite green)
- [x] `streamablehttp_client` 3-tuple correctly unpacked
- [x] Changed-fingerprint tools absent from `ToolRegistry` (not registered, not a gate message)
- [x] Spawn-denied servers set `state = "denied"` and do not enter the stdio context manager
- [x] Design doc updated with OAuth setup steps, rug-pull behavior, spawn consent, and curl examples using correct port 8765
- [x] Conventional Commits, no Co-Authored-By trailer, explicit `git add` paths only
- [ ] Manual smoke-test performed with at least one real remote OAuth server *(user to perform during smoke-testing)*

### Assumptions & open questions carried forward

1. **`token_endpoint_auth_method="none"`** — assumes all OAuth 2.1 MCP servers registered
   by Jack are public clients (PKCE, no `client_secret`). If a server requires
   `client_secret_basic`, add it as a config field (`auth.client_auth_method`) and thread it
   into `OAuthClientMetadata`. Deferred to a follow-up issue.

2. **`client_metadata_url`** — left as `None` (dynamic client registration via DCR).
   If a server supports `client_id_metadata_document_supported=true`, the user can configure
   `auth.client_metadata_url` in `servers.json`; `_build_oauth_provider` would pass it
   through. Not implemented in Phase 6 to keep scope bounded.

3. **Re-consent UX (rug-pull) is one-sided in Phase 6.** The Python side marks the tool
   absent and emits `mcp_tool_changed`; the UI can surface a "tool changed — review"
   indicator. A full diff card (old description vs new) requires the worker to persist the old
   tool description alongside the fingerprint — deferred to a follow-up issue.

4. **`approved.json` is mode 0600.** The fingerprints contain hashes of public tool schemas
   (not sensitive). Spawn records contain `command + args` (which may include local paths).
   Mode 0600 is set; acceptable for the threat model.

5. **`McpManager.set_confirmer()`** is a setter rather than a constructor parameter to keep
   backward compatibility with tests that construct `McpManager` without a confirmer. The
   default (no confirmer) auto-approves spawns (existing behavior), so no existing test breaks.

---

### OAuth with a pre-registered app (servers without dynamic registration)

Some MCP servers — notably **Slack** (`mcp.slack.com`) and **GitHub**
(`api.githubcopilot.com/mcp`) — do not publish a `registration_endpoint` in their
authorization server metadata. The SDK's Dynamic Client Registration (DCR) fallback
returns a 404. These servers require a **pre-registered OAuth app**: a `client_id`
(and, for Slack, a `client_secret`).

**Why the redirect URI must be fixed.** The registered redirect URI must match
exactly. Jack therefore uses a fixed loopback port for pre-registered apps:

```
http://127.0.0.1:8975/callback
```

Register this URL exactly as-is in your OAuth app's settings.

**Slack setup** (`mcp.slack.com`)

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app (or use an existing one).
2. Under **OAuth & Permissions**, add `http://127.0.0.1:8975/callback` to **Redirect URLs**.
3. Copy the **Client ID** and **Client Secret** from **Basic Information**.
4. User scopes come from the MCP server's own metadata — you do not need to add them manually.
5. In the Jack wizard (step 4, OAuth), enter the Client ID and Client Secret, then click **Open browser**.

**GitHub setup** (`api.githubcopilot.com/mcp`)

1. Go to [github.com/settings/developers](https://github.com/settings/developers) → **New OAuth App**.
2. Set the **Authorization callback URL** to `http://127.0.0.1:8975/callback`.
3. Copy the **Client ID** and click **Generate a new client secret** — copy the secret immediately.
4. In the Jack wizard (step 4, OAuth), enter the Client ID and Client Secret, then click **Open browser**.

**What is stored where.** The `client_id` is persisted in `~/.autobot/mcp/servers.json`
(config, non-secret). The `client_secret` is stored in the macOS Keychain under
`mcp.<id>.client_secret` — never written to disk. OAuth tokens are stored in the
Keychain under `mcp.<id>.oauth` as before.

**Live flow.** The live OAuth flow is a manual smoke-test; it cannot be automated
in unit tests because it requires a real browser and a real authorization server.
After clicking **Open browser**, Jack opens the provider's authorization URL, waits
on port 8975 for the redirect callback, exchanges the code for tokens, and marks
the server connected.
