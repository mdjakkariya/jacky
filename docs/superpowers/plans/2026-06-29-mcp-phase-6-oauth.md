# MCP Phase 6 — OAuth 2.1, HTTP Transport, Fingerprint Re-consent, Local-Spawn Consent

**Date:** 2026-06-29
**Branch:** `feat/mcp-integration`
**Scope:** Final phase. Four independent but related deliverables, implemented
in sequence because each one gates the next: OAuth building blocks → HTTP transport
branch → fingerprint rug-pull protection → local-spawn consent → daemon `auth/start`
endpoint → design-doc update.

---

## Step 0 — Verified SDK Signatures (mcp 1.28.0)

Run before any code: these signatures are ground truth. The plan uses them
verbatim. If a future upgrade changes them, re-verify before coding.

```
OAuthClientProvider.__init__ parameters:
  self, server_url, client_metadata, storage, redirect_handler,
  callback_handler, timeout, client_metadata_url

TokenStorage methods (abstract, async):
  get_tokens()  -> OAuthToken | None
  set_tokens(tokens: OAuthToken) -> None
  get_client_info() -> OAuthClientInformationFull | None
  set_client_info(info: OAuthClientInformationFull) -> None

OAuthClientMetadata fields:
  redirect_uris (required, list[str], min 1)
  token_endpoint_auth_method, grant_types, response_types, scope,
  client_name, client_uri, logo_uri, contacts, tos_uri, policy_uri,
  jwks_uri, jwks, software_id, software_version

OAuthToken fields:
  access_token, token_type, expires_in, scope, refresh_token

OAuthClientInformationFull fields:
  (all OAuthClientMetadata fields) + client_id, client_secret,
  client_id_issued_at, client_secret_expires_at

streamablehttp_client parameters:
  url, headers, timeout, sse_read_timeout, terminate_on_close,
  httpx_client_factory, auth

callback_handler return type: tuple[str, str | None]  # (auth_code, state)
redirect_handler return type: Awaitable[None]
```

---

## Context

### What already exists (do not break)

- `src/autobot/mcp/session.py` — `McpServerWorker.run()` is **stdio-only** today.
  It enters `stdio_client(params) as (read, write)` then `ClientSession(read, write)`.
  The HTTP branch adds a parallel `if cfg.transport == "http":` arm.
- `src/autobot/mcp/auth.py` — contains only `stdio_env_for` (pure, no SDK import).
  OAuth building blocks are added here.
- `src/autobot/secrets.py` — `get_secret`, `set_secret`, `delete_secret`, `has_secret`.
  Tokens persist as JSON blobs under Keychain account names `mcp.<id>.oauth` (OAuthToken)
  and `mcp.<id>.client` (OAuthClientInformationFull). Never log values.
- `src/autobot/mcp/adapter.py` — `fingerprint(tool)` exists (sha256 of
  name + description + inputSchema + annotations). Already imported by session.py.
- `src/autobot/mcp/manager.py` — `McpManager`. No changes to its public API;
  internal state only (spawn-consent store, finger print store). The manager's
  background loop stays as-is.
- `src/autobot/daemon/server.py` — `post_mcp_auth_start` is a stub returning
  `{"ok": False, "error": "oauth not yet supported (phase 6)"}`. Phase 6 wires it.
- `src/autobot/tools/permission.py` — `Confirmer` protocol and `PermissionGate`.
  Spawn-consent reuses `Confirmer.confirm()`; nothing changes inside the gate.

### Naming conventions for Keychain accounts

| Purpose | Account name |
|---------|--------------|
| OAuth token bundle | `mcp.<id>.oauth` |
| OAuth client registration | `mcp.<id>.client` |
| Static bearer token | `mcp.<id>.token` (Phase 3, unchanged) |
| Spawn approval record | persisted as a sidecar JSON file (see Task 4) |

Approved fingerprints and spawn-consent records are NOT secrets. They are stored as
a sidecar JSON file `~/.autobot/mcp/approved.json` (0600, created on first write).

---

## Global Constraints (mandatory, enforced by `make check`)

- Python >= 3.11; `from __future__ import annotations` in every module.
- Full type hints; **mypy runs in `strict` mode** — keep it green over `src/` AND `tests/`.
- Google-style docstrings on public modules, classes, functions (tests exempt).
- Line length 100; formatting/imports owned by `ruff` — run `make format` before committing.
- Value objects are `frozen=True, slots=True` dataclasses. No business logic on them.
- **On-device / privacy:** tokens ONLY in Keychain (never logged, printed, or in
  `servers.json` — only `secret_ref` names are stored on disk). OAuth callback is
  loopback-only (`127.0.0.1:0`). `redirect_handler` must validate scheme is http/https
  before opening, must NOT use a shell.
- mcp SDK **lazy-imported** inside methods; OAuth-touching integration tests use
  `pytest.importorskip("mcp")` and run via `uv run --extra mcp pytest tests/integration/`.
  Base `make check` (no extra) must stay green — the `mcp.*` mypy override already covers SDK types.
- **Conventional Commits. NO `Co-Authored-By` or attribution trailer.**
- Stage explicit paths only (never `git add -A`).
- Verification gate per task: `make check` green. Tasks touching HTTP/OAuth integration
  also require: `uv run --extra mcp pytest tests/integration/ -v`.
- **Live OAuth flow is a MANUAL SMOKE-TEST** (no real auth server in CI). Unit tests
  cover building blocks + construction + fingerprint/spawn-consent logic; the live
  browser redirect + token exchange is documented at the end of Task 6 (design doc).

---

## Architecture — What Phase 6 Adds

```
McpServerWorker.run()
  ├── transport == "stdio"  (unchanged)
  │     stdio_client(params) → (read, write)
  └── transport == "http"   (NEW)
        auth_type == "oauth2"
          OAuthClientProvider(
            server_url=cfg.url,
            client_metadata=OAuthClientMetadata(...),
            storage=KeychainTokenStorage(cfg.id),
            redirect_handler=open_browser,
            callback_handler=LoopbackCallbackServer().wait,
          ) → auth object
          streamablehttp_client(cfg.url, auth=auth) → (read, write, get_session_id)
        auth_type == "token"
          token = get_secret(cfg.secret_ref)
          streamablehttp_client(cfg.url, headers={"Authorization": f"Bearer {token}"})
                                         → (read, write, get_session_id)
        auth_type == "none"
          streamablehttp_client(cfg.url) → (read, write, get_session_id)
        # all three paths unpack only (read, write) and pass to ClientSession

McpServerWorker._sync_tools()  (EXTENDED)
  - compare each enabled tool's fingerprint vs approved.json
  - if changed → mark pending-reconsent, emit mcp event, skip registration
  - if new → auto-approve (policy: new tools from a previously-approved server
    are registered with a "new-tool" flag; the gate lets them run but emits a
    notice event so the UI can surface them for review)

McpManager.connect() / McpServerWorker.run()  (EXTENDED — stdio only)
  - before FIRST spawn: read approved.json for spawn approval
  - if not approved → call confirmer.confirm(exact_command_display)
  - if declined → raise / set state "denied" / do not spawn
  - if approved → persist to approved.json, then spawn

Daemon server.py — post_mcp_auth_start()  (WIRED)
  - call mcp.start_oauth(server_id)  (new McpManager method)
  - McpManager.start_oauth() triggers a connect that will run the OAuth flow
    (browser + loopback) and emit mcp_oauth stage events through on_event
```

---

## Approved.json schema

```json
{
  "fingerprints": {
    "<server_id>": {
      "<namespaced_tool_name>": "<sha256_hex>"
    }
  },
  "spawn_approvals": {
    "<server_id>": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/..."],
      "approved_at": "2026-06-29T12:00:00Z"
    }
  }
}
```

Stored at `~/.autobot/mcp/approved.json`, mode 0600. A helper module
`src/autobot/mcp/approvals.py` owns all reads/writes so the format is in one place.

---

## Tasks

### Task 1 — OAuth building blocks: `KeychainTokenStorage`, `open_browser`, `LoopbackCallbackServer`

**Files to create/edit:**
- `src/autobot/mcp/auth.py` — add the three OAuth classes
- `tests/unit/test_mcp_auth.py` — extend (do not replace existing tests)

#### TDD steps

**Write tests first in `tests/unit/test_mcp_auth.py`:**

```python
# Test: KeychainTokenStorage.get_tokens returns None when Keychain is empty
# Test: KeychainTokenStorage.set_tokens round-trips through the fake runner
# Test: KeychainTokenStorage.get_client_info returns None when absent
# Test: KeychainTokenStorage.set_client_info round-trips
# Test: open_browser raises ValueError for non-http/https scheme
# Test: open_browser calls webbrowser.open (monkeypatch) for valid https URL
# Test: LoopbackCallbackServer parses code+state from one simulated request
# Test: LoopbackCallbackServer times out with asyncio.TimeoutError after deadline
```

**Implementation in `src/autobot/mcp/auth.py`:**

```python
"""Token injection helpers for MCP server connections.

Adds Phase 6: KeychainTokenStorage (Keychain-backed TokenStorage), open_browser
(scheme-validated, non-shell URL opener), and LoopbackCallbackServer (loopback
OAuth callback listener). The mcp SDK is lazy-imported in KeychainTokenStorage
methods so this module stays importable without the extra.
"""

from __future__ import annotations

import asyncio
import json
import webbrowser
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autobot.mcp.config import McpServerConfig
    from autobot.secrets import Runner


def stdio_env_for(
    cfg: McpServerConfig,
    get_secret: Callable[[str], str | None],
) -> dict[str, str] | None:
    # ... (unchanged) ...


class KeychainTokenStorage:
    """Async TokenStorage backed by the macOS Keychain.

    Token values are serialized as JSON blobs stored under:
    - ``mcp.<server_id>.oauth``  → OAuthToken
    - ``mcp.<server_id>.client`` → OAuthClientInformationFull

    The mcp SDK is lazy-imported inside each method so importing this module
    does not require the ``mcp`` extra. Values are never logged.

    Args:
        server_id: The MCP server's config id (e.g. ``"github"``).
        runner: Optional Keychain runner for testing (defaults to subprocess).
    """

    def __init__(self, server_id: str, runner: Runner | None = None) -> None:
        self._token_key = f"mcp.{server_id}.oauth"
        self._client_key = f"mcp.{server_id}.client"
        self._runner = runner

    async def get_tokens(self) -> object | None:  # return type: OAuthToken | None
        """Return the stored OAuthToken, or None if absent/unparseable."""
        from mcp.shared.auth import OAuthToken
        from autobot.secrets import get_secret
        raw = get_secret(self._token_key, self._runner)
        if raw is None:
            return None
        try:
            return OAuthToken.model_validate(json.loads(raw))
        except Exception:
            return None

    async def set_tokens(self, tokens: object) -> None:
        """Persist an OAuthToken to the Keychain as JSON. Never logs the value."""
        from autobot.secrets import set_secret
        raw = tokens.model_dump_json()  # type: ignore[attr-defined]
        set_secret(self._token_key, raw, self._runner)

    async def get_client_info(self) -> object | None:  # OAuthClientInformationFull | None
        """Return the stored OAuthClientInformationFull, or None."""
        from mcp.shared.auth import OAuthClientInformationFull
        from autobot.secrets import get_secret
        raw = get_secret(self._client_key, self._runner)
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(json.loads(raw))
        except Exception:
            return None

    async def set_client_info(self, info: object) -> None:
        """Persist OAuthClientInformationFull to the Keychain. Never logs the value."""
        from autobot.secrets import set_secret
        raw = info.model_dump_json()  # type: ignore[attr-defined]
        set_secret(self._client_key, raw, self._runner)


def open_browser(url: str) -> None:
    """Open ``url`` in the default browser (validated, non-shell).

    Only ``http`` and ``https`` schemes are accepted. Uses ``webbrowser.open``
    (no shell, no subprocess). Raises ``ValueError`` for any other scheme so a
    malicious server cannot inject a ``file://`` or custom-scheme URL.

    Args:
        url: The authorization URL returned by the OAuth server.

    Raises:
        ValueError: If the URL scheme is not ``http`` or ``https``.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"open_browser: refusing non-http/https URL scheme={parsed.scheme!r}"
        )
    webbrowser.open(url)


_LOOPBACK_TIMEOUT_S = 120.0  # 2 minutes; user must complete the browser flow


class LoopbackCallbackServer:
    """One-shot loopback HTTP server that captures the OAuth callback.

    Binds to ``127.0.0.1:0`` (OS-assigned port), serves exactly one GET request,
    parses ``code`` and ``state`` from the query string, then closes. Designed to
    be used as the ``callback_handler`` for ``OAuthClientProvider``.

    Usage::

        server = LoopbackCallbackServer()
        redirect_uri = await server.start()
        # pass redirect_uri to OAuthClientProvider's client_metadata.redirect_uris
        code, state = await server.wait()  # blocks until callback or timeout
    """

    def __init__(self, timeout: float = _LOOPBACK_TIMEOUT_S) -> None:
        self._timeout = timeout
        self._port: int | None = None
        self._result: asyncio.Future[tuple[str, str | None]] | None = None

    async def start(self) -> str:
        """Bind the server socket and return the redirect URI.

        Returns:
            The redirect URI to register: ``http://127.0.0.1:<port>/callback``.
        """
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self._port = sock.getsockname()[1]
        sock.close()
        loop = asyncio.get_event_loop()
        self._result = loop.create_future()
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self._port
        )
        return f"http://127.0.0.1:{self._port}/callback"

    async def wait(self) -> tuple[str, str | None]:
        """Block until the callback arrives or the timeout expires.

        Returns:
            ``(auth_code, state)`` — ``state`` may be None if the server omits it.

        Raises:
            asyncio.TimeoutError: If no callback arrives within ``timeout`` seconds.
        """
        assert self._result is not None, "call start() before wait()"
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._result), timeout=self._timeout
            )
        finally:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Parse the GET /callback?code=...&state=... and resolve the future."""
        from urllib.parse import parse_qs, urlparse
        try:
            line = (await reader.readline()).decode()
            # e.g. "GET /callback?code=abc&state=xyz HTTP/1.1"
            path = line.split(" ")[1] if " " in line else ""
            params = parse_qs(urlparse(path).query)
            code = (params.get("code") or [""])[0]
            state_vals = params.get("state")
            state: str | None = state_vals[0] if state_vals else None
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n"
                b"Authorized. You may close this tab.\n"
            )
            await writer.drain()
        finally:
            writer.close()
        if self._result is not None and not self._result.done():
            if code:
                self._result.set_result((code, state))
            else:
                self._result.set_exception(
                    ValueError("OAuth callback missing 'code' parameter")
                )
```

**Exact mypy annotations:** `get_tokens` / `get_client_info` return `object | None` to
avoid importing the SDK at the method signature level (strict mode). Inside the body,
the type is validated via `model_validate`; callers that need the concrete type annotate
with `TYPE_CHECKING` guards. Alternatively, use `Any` with a `# type: ignore[return-value]`
and a comment explaining the lazy import constraint.

**Verification:**
```bash
make check
# Expected: all green, no new mypy errors
```

---

### Task 2 — HTTP transport branch in `McpServerWorker.run()`

**Files to edit:**
- `src/autobot/mcp/session.py` — add the `http` branch inside `run()`
- `tests/unit/test_mcp_session.py` — add construction-level tests for the HTTP path

#### Context

`streamablehttp_client` yields a **3-tuple** `(read, write, get_session_id)`, unlike
`stdio_client`'s 2-tuple `(read, write)`. Both are unpacked and only `read, write` are
passed to `ClientSession`. The `get_session_id` callable is discarded (no current use).

#### TDD steps

**Write tests first in `tests/unit/test_mcp_session.py`:**

```python
# Test: _make_oauth_provider returns OAuthClientProvider with correct server_url
#       (use pytest.importorskip("mcp"); verify type and that storage is KeychainTokenStorage)
# Test: _make_oauth_provider uses redirect_uris from LoopbackCallbackServer start()
#       (the actual port assignment; verify format "http://127.0.0.1:<port>/callback")
# Test: _http_headers returns {"Authorization": "Bearer <token>"} when auth_type=="token"
# Test: _http_headers returns {} when auth_type=="none" (no secret_ref)
```

These tests verify the *construction* path, not the live flow. The live flow (browser +
real OAuth server) is a manual smoke-test (see Task 6).

#### Implementation in `src/autobot/mcp/session.py`

Add a private helper `_build_http_transport` and call it from `run()`:

```python
async def run(self) -> None:
    """Connect, register tools, serve calls until shutdown, then clean up."""
    self._queue = asyncio.Queue()
    try:
        if self._cfg.transport == "http":
            await self._run_http()
        else:
            await self._run_stdio()
    except Exception as exc:
        self._state = "error"
        _log.exception("mcp worker failed server=%s", self._cfg.id)
        self._emit_status(error=str(exc))
    finally:
        if self._state == "connected":
            self._state = "disconnected"
        self._fail_pending()
        self._unregister_all()
        self._tool_count = 0
        self._all_tools = []
        self._emit_status()
        _log.info("mcp disconnected server=%s", self._cfg.id)

async def _run_stdio(self) -> None:
    """Stdio branch (unchanged logic, extracted for clarity)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=self._cfg.command or "",
        args=list(self._cfg.args),
        env=stdio_env_for(self._cfg, _get_secret),
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write, message_handler=self._on_message) as session,
    ):
        await session.initialize()
        await self._sync_tools(session)
        self._state = "connected"
        self._emit_status()
        _log.info("mcp connected server=%s tools=%d", self._cfg.id, self._tool_count)
        await self._serve(session)

async def _run_http(self) -> None:
    """HTTP transport branch: OAuth2, static bearer token, or unauthenticated."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = self._cfg.url or ""
    auth_type = self._cfg.auth_type

    if auth_type == "oauth2":
        auth = await self._build_oauth_provider()
        cm = streamablehttp_client(url, auth=auth)
    elif auth_type == "token":
        token = _get_secret(self._cfg.secret_ref or "") if self._cfg.secret_ref else None
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        cm = streamablehttp_client(url, headers=headers)
    else:
        cm = streamablehttp_client(url)

    async with cm as (read, write, _get_session_id):
        async with ClientSession(read, write, message_handler=self._on_message) as session:
            await session.initialize()
            await self._sync_tools(session)
            self._state = "connected"
            self._emit_status()
            _log.info("mcp connected server=%s transport=http tools=%d",
                      self._cfg.id, self._tool_count)
            await self._serve(session)

async def _build_oauth_provider(self) -> Any:
    """Construct an OAuthClientProvider for this server.

    Starts the loopback callback server (OS-assigned port), builds the client
    metadata with the loopback redirect URI, and wires KeychainTokenStorage and
    open_browser. The provider is returned to _run_http which passes it as
    ``auth=`` to streamablehttp_client.

    Returns:
        An OAuthClientProvider instance (an httpx.Auth subclass).
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    from autobot.mcp.auth import KeychainTokenStorage, LoopbackCallbackServer, open_browser

    cb_server = LoopbackCallbackServer()
    redirect_uri = await cb_server.start()

    storage = KeychainTokenStorage(self._cfg.id)
    metadata = OAuthClientMetadata(
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="Jack",
        token_endpoint_auth_method="none",  # public client (PKCE, no client_secret)
    )

    async def redirect_handler(url: str) -> None:
        _log.info("mcp oauth redirect server=%s", self._cfg.id)
        self._emit_oauth_stage("browser_open")
        open_browser(url)

    async def callback_handler() -> tuple[str, str | None]:
        self._emit_oauth_stage("waiting_callback")
        result = await cb_server.wait()
        self._emit_oauth_stage("callback_received")
        return result

    return OAuthClientProvider(
        server_url=self._cfg.url or "",
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

def _emit_oauth_stage(self, stage: str) -> None:
    """Publish an mcp_oauth event for the UI (never raises)."""
    self._emit_event({
        "type": "mcp_oauth",
        "server": self._cfg.id,
        "stage": stage,
    })

def _emit_event(self, payload: dict[str, Any]) -> None:
    """Publish any structured event (shared helper; never raises)."""
    if self._on_event is None:
        return
    try:
        self._on_event(payload)
    except Exception:
        _log.debug("mcp on_event sink failed", exc_info=True)
```

Note: refactor `_emit_status` to call `_emit_event` internally so the two-sink
pattern is in one place.

**Verification:**
```bash
make check
uv run --extra mcp pytest tests/unit/test_mcp_session.py -v
# Expected: construction-level tests green; mypy strict green
```

---

### Task 3 — Fingerprint re-consent / rug-pull protection

**Files to create/edit:**
- `src/autobot/mcp/approvals.py` — new module: `load_approvals`, `save_approvals`,
  typed `ApprovalsFile` dataclass; fingerprints + spawn approvals in one file
- `src/autobot/mcp/session.py` — extend `_sync_tools` with fingerprint compare
- `tests/unit/test_mcp_approvals.py` — new unit tests
- `tests/unit/test_mcp_session.py` — add fingerprint-changed path tests

#### `src/autobot/mcp/approvals.py`

```python
"""Persistence for approved tool fingerprints and spawn approvals.

All data lives in ``~/.autobot/mcp/approved.json`` (0600). Tokens are NOT here
(they're in the Keychain). This file tracks what the user has explicitly consented
to, so a rug-pull (silently changed tool definition) or a new spawn are surfaced
rather than silently executed.

Schema::

    {
      "fingerprints": {"<server_id>": {"<namespaced_tool>": "<sha256>"}},
      "spawn_approvals": {
        "<server_id>": {"command": "...", "args": [...], "approved_at": "..."}
      }
    }
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_APPROVALS_PATH = "~/.autobot/mcp/approved.json"


@dataclass
class SpawnApproval:
    """A user-approved command + args for a stdio server spawn."""

    command: str
    args: list[str]
    approved_at: str


@dataclass
class ApprovalsFile:
    """In-memory view of approved.json."""

    fingerprints: dict[str, dict[str, str]] = field(default_factory=dict)
    spawn_approvals: dict[str, SpawnApproval] = field(default_factory=dict)


def load_approvals(path: str | Path = DEFAULT_APPROVALS_PATH) -> ApprovalsFile:
    """Load approved.json; return empty ApprovalsFile on missing or malformed."""
    p = Path(path).expanduser()
    if not p.exists():
        return ApprovalsFile()
    try:
        data: Any = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ApprovalsFile()
    fps: dict[str, dict[str, str]] = {}
    for sid, tools in (data.get("fingerprints") or {}).items():
        if isinstance(tools, dict):
            fps[str(sid)] = {str(k): str(v) for k, v in tools.items()}
    spawns: dict[str, SpawnApproval] = {}
    for sid, rec in (data.get("spawn_approvals") or {}).items():
        if isinstance(rec, dict) and rec.get("command"):
            spawns[str(sid)] = SpawnApproval(
                command=str(rec["command"]),
                args=[str(a) for a in (rec.get("args") or [])],
                approved_at=str(rec.get("approved_at", "")),
            )
    return ApprovalsFile(fingerprints=fps, spawn_approvals=spawns)


def save_approvals(
    af: ApprovalsFile, path: str | Path = DEFAULT_APPROVALS_PATH
) -> None:
    """Persist ApprovalsFile to approved.json (0600)."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "fingerprints": af.fingerprints,
        "spawn_approvals": {
            sid: {
                "command": sp.command,
                "args": sp.args,
                "approved_at": sp.approved_at,
            }
            for sid, sp in af.spawn_approvals.items()
        },
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    with contextlib.suppress(OSError):
        p.chmod(0o600)


def record_fingerprints(
    server_id: str,
    tool_fingerprints: dict[str, str],
    path: str | Path = DEFAULT_APPROVALS_PATH,
) -> None:
    """Merge ``tool_fingerprints`` into approved.json for ``server_id``."""
    af = load_approvals(path)
    af.fingerprints.setdefault(server_id, {}).update(tool_fingerprints)
    save_approvals(af, path)


def record_spawn_approval(
    server_id: str,
    command: str,
    args: list[str],
    path: str | Path = DEFAULT_APPROVALS_PATH,
) -> None:
    """Mark a stdio spawn as approved (idempotent — overwrites on re-approval)."""
    af = load_approvals(path)
    af.spawn_approvals[server_id] = SpawnApproval(
        command=command,
        args=args,
        approved_at=datetime.now(timezone.utc).isoformat(),
    )
    save_approvals(af, path)
```

#### Extension to `_sync_tools` in `session.py`

The fingerprint gating logic operates on the **per-tool** level. Policy:

- **Tool fingerprint unchanged AND already in approved.json → register normally.**
- **Tool fingerprint changed vs approved.json → do NOT register; mark `pending_reconsent=True`
  in `_all_tools`; emit an `mcp_tool_changed` event so the UI can show a diff card.
  Gate will return a failed ToolResult for calls to this tool ("tool requires re-consent").**
- **New tool (not in approved.json for this server) → auto-register (register with `replace=True`)
  AND add its fingerprint to approved.json (the first listing is the baseline). This is the
  "new server, first connect" case. Emit an `mcp_new_tool` notice event.**

```python
# In _sync_tools, after building desired dict:

from autobot.mcp.approvals import load_approvals, record_fingerprints
from autobot.mcp import adapter

approvals = load_approvals()
approved_fps = approvals.fingerprints.get(self._cfg.id, {})
new_fps: dict[str, str] = {}
reconsent_names: list[str] = []

for name, spec in list(desired.items()):
    # Recompute the fingerprint for the live tool object
    # (we need the raw tool object; extend desired to store it)
    fp = _compute_fp_for(name)  # stored alongside spec in a parallel dict
    if name in approved_fps:
        if approved_fps[name] != fp:
            # Rug pull detected
            desired.pop(name)
            reconsent_names.append(name)
            _log.warning(
                "mcp tool fingerprint changed server=%s tool=%s — blocking pending re-consent",
                self._cfg.id, name,
            )
        # else: unchanged, allow through
    else:
        # New tool: auto-approve, baseline its fingerprint
        new_fps[name] = fp

if new_fps:
    record_fingerprints(self._cfg.id, new_fps)
if reconsent_names:
    self._emit_event({
        "type": "mcp_tool_changed",
        "server": self._cfg.id,
        "tools": reconsent_names,
    })
```

Implementation note: to pass the raw MCP tool objects through `_sync_tools` alongside the
`ToolSpec`, build a parallel `dict[str, _ToolLike]` called `tool_objects` inside `_sync_tools`
and call `adapter.fingerprint(tool_objects[name])` when computing `fp`.

Mark changed tools in `_all_tools` with `"pending_reconsent": True`.

#### TDD (test_mcp_session.py additions)

```python
# Test: _sync_tools with fingerprint unchanged → tool registered normally
# Test: _sync_tools with fingerprint changed → tool NOT registered; mcp_tool_changed emitted
# Test: _sync_tools new tool (not in approvals) → auto-registered; fingerprint written
```

Use a fake approvals path (tmp_path fixture) so no real file is touched.

**Verification:**
```bash
make check
uv run --extra mcp pytest tests/unit/test_mcp_session.py tests/unit/test_mcp_approvals.py -v
```

---

### Task 4 — Local-spawn consent for stdio servers

**Files to edit:**
- `src/autobot/mcp/session.py` — check spawn approval before `_run_stdio()`
- `src/autobot/mcp/manager.py` — thread `confirmer` into `McpServerWorker` construction
- `src/autobot/tools/permission.py` — no changes (reuse `Confirmer` protocol as-is)
- `tests/unit/test_mcp_session.py` — spawn-consent tests

#### Design

Before the **first** `_run_stdio()` spawn for a server, `McpServerWorker` checks
`approved.json` for a spawn approval matching `(command, args)`. If absent, it calls
`confirmer.confirm(prompt)` where `prompt` shows the **exact, untruncated** command + args.
If the user approves, the approval is written to `approved.json` and the spawn proceeds.
If denied, the worker sets `_state = "denied"` and returns without spawning.

The approval persists so subsequent connects (e.g. after a crash) skip the prompt.
If the command/args change (e.g. user edits `servers.json`), the old approval no longer
matches and consent is re-requested.

#### Worker changes

Add `confirmer` as an optional constructor parameter:

```python
class McpServerWorker:
    def __init__(
        self,
        config: McpServerConfig,
        registry: ToolRegistry,
        *,
        loop: asyncio.AbstractEventLoop,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        confirmer: Confirmer | None = None,        # NEW
        approvals_path: str | Path | None = None,  # NEW (injection for tests)
    ) -> None:
        ...
        self._confirmer = confirmer
        self._approvals_path = approvals_path
```

In `run()`, before calling `_run_stdio()`:

```python
if self._cfg.transport == "stdio":
    if not await self._check_spawn_consent():
        self._state = "denied"
        _log.info("mcp spawn denied by user server=%s", self._cfg.id)
        self._emit_status(error="spawn denied by user")
        return
    await self._run_stdio()
```

```python
async def _check_spawn_consent(self) -> bool:
    """Return True if spawn is approved; False if denied.

    Checks approved.json first; if not approved, asks via confirmer (if wired).
    Falls back to True (auto-allow) when no confirmer is provided (non-interactive
    use, tests, or when consent was already granted).

    IMPORTANT: ``confirmer.confirm`` is a BLOCKING call (it waits for the user via
    the card/voice). This method runs on the manager's event loop, so the confirm
    MUST run via ``run_in_executor`` — calling it inline would freeze the loop
    (and every other server's worker + the message handler) until the user answers.
    """
    import asyncio
    from autobot.mcp.approvals import load_approvals, record_spawn_approval

    path = self._approvals_path or DEFAULT_APPROVALS_PATH
    af = load_approvals(path)
    existing = af.spawn_approvals.get(self._cfg.id)
    command = self._cfg.command or ""
    args = list(self._cfg.args)

    if existing is not None and existing.command == command and existing.args == args:
        return True  # previously approved

    if self._confirmer is None:
        # No UI confirmer: auto-approve (first-run headless / unit test without confirmer)
        record_spawn_approval(self._cfg.id, command, args, path)
        return True

    args_display = " ".join(args)
    prompt = (
        f"Allow Jack to launch this process?\n\n"
        f"  {command} {args_display}\n\n"
        f"This will run as your user account."
    )
    # Run the blocking confirm OFF the loop thread so the loop stays responsive.
    loop = asyncio.get_running_loop()
    approved = await loop.run_in_executor(None, self._confirmer.confirm, prompt, "write")
    if approved:
        record_spawn_approval(self._cfg.id, command, args, path)
    return approved
```

(The spawn-consent unit tests must therefore `await` `_check_spawn_consent()` inside an async test or via `asyncio.run`, and may use a synchronous fake confirmer — `run_in_executor` runs it on a worker thread and returns its result.)

#### Manager changes

Thread `confirmer` through `McpManager.connect()`:

```python
# In McpManager.__init__:
self._confirmer: Confirmer | None = None  # set via set_confirmer()

def set_confirmer(self, confirmer: Confirmer) -> None:
    """Wire a Confirmer for spawn consent prompts."""
    with self._lock:
        self._confirmer = confirmer

# In McpManager.connect(), when constructing McpServerWorker:
worker = McpServerWorker(
    cfg, self._registry,
    loop=self._loop,
    on_event=self._on_event,
    confirmer=self._confirmer,
)
```

`app.py::build()` sets the confirmer on the manager after constructing both.

#### TDD (test_mcp_session.py additions)

```python
# Test: _check_spawn_consent with existing matching approval → True (no confirm called)
# Test: _check_spawn_consent with no approval, AlwaysAllow confirmer → True; approval written
# Test: _check_spawn_consent with no approval, AlwaysDeny confirmer → False; no approval written
# Test: _check_spawn_consent with changed args (old approval with different args) → re-prompts
```

Use `tmp_path` for `approvals_path` to avoid touching the real file.

**Verification:**
```bash
make check
uv run --extra mcp pytest tests/unit/test_mcp_session.py -v
```

---

### Task 5 — Wire `POST /mcp/servers/{id}/auth/start` in `server.py`

**Files to edit:**
- `src/autobot/mcp/manager.py` — add `start_oauth(server_id: str) -> dict[str, Any]`
- `src/autobot/daemon/server.py` — replace the stub
- `tests/unit/test_daemon_server.py` — replace/extend the stub test

#### `McpManager.start_oauth`

```python
def start_oauth(self, server_id: str) -> dict[str, Any]:
    """Trigger the OAuth flow for an oauth2 HTTP server.

    Connects (or reconnects) the server, which will trigger the browser-open +
    loopback callback flow on the worker's event loop and emit ``mcp_oauth``
    stage events through ``on_event``. The method is non-blocking: it schedules
    the connect and returns immediately. The UI polls ``mcp_status`` and listens
    for ``mcp_oauth`` events.

    Args:
        server_id: The server's config id.

    Returns:
        ``{"ok": True, "started": True}`` or ``{"ok": False, "error": str}``.
    """
    with self._lock:
        cfg = self._config.get(server_id)
        if cfg is None:
            return {"ok": False, "error": f"unknown server: {server_id!r}"}
        if cfg.transport != "http":
            return {"ok": False, "error": "auth/start only applies to http transport"}
        if cfg.auth_type != "oauth2":
            return {"ok": False, "error": f"server {server_id!r} is not oauth2"}
        # Disconnect first (so a re-auth re-runs the flow even if already connected)
        if server_id in self._workers:
            self.disconnect(server_id)
        self.connect(server_id)
    return {"ok": True, "started": True}
```

#### `server.py` stub replacement

```python
async def post_mcp_auth_start(server_id: str) -> dict[str, Any]:
    """Initiate the OAuth 2.1 flow for an oauth2 HTTP server.

    Disconnects any existing worker and reconnects, which triggers the
    browser-open + loopback callback inside the worker's event loop.
    Stage events (``mcp_oauth``) are published via the WS event bus so the
    UI can show a progress indicator. Non-blocking: returns immediately.
    """
    if mcp is None:
        return _mcp_disabled
    return await asyncio.to_thread(mcp.start_oauth, server_id)
```

#### TDD (test_daemon_server.py)

```python
# Test: POST /mcp/servers/gh/auth/start with non-oauth2 server → {"ok": False, "error": ...}
# Test: POST /mcp/servers/gh/auth/start with unknown server → {"ok": False, "error": ...}
# Test: POST /mcp/servers/gh/auth/start with oauth2 http server → {"ok": True, "started": True}
#       (mock manager.start_oauth to return the dict; verify the endpoint passes it through)
```

**Verification:**
```bash
make check
uv run --extra mcp pytest tests/unit/test_daemon_server.py -v -k auth
```

---

### Task 6 — Integration smoke-test + design-doc update

**Files to edit:**
- `docs/plans/mcp-integration-design.md` — append Phase 6 section

No Python code changes. This task documents what Task 1-5 built and tells the user
how to manually smoke-test the live OAuth flow.

#### Content to append to `docs/plans/mcp-integration-design.md`

Add a "## Phase 6 — OAuth 2.1, HTTP Transport, Rug-Pull & Spawn Consent" section:

```markdown
## Phase 6 — OAuth 2.1, HTTP Transport, Rug-Pull & Spawn Consent

### What was built

| Deliverable | File(s) |
|-------------|---------|
| KeychainTokenStorage | `src/autobot/mcp/auth.py` |
| open_browser (scheme-validated) | `src/autobot/mcp/auth.py` |
| LoopbackCallbackServer (127.0.0.1:0) | `src/autobot/mcp/auth.py` |
| HTTP transport branch (OAuth / token / none) | `src/autobot/mcp/session.py` |
| Fingerprint rug-pull detection | `src/autobot/mcp/session.py` + `approvals.py` |
| Local-spawn consent | `src/autobot/mcp/session.py` + `approvals.py` |
| approved.json persistence | `src/autobot/mcp/approvals.py` |
| auth/start endpoint (wired) | `src/autobot/daemon/server.py` |

### OAuth setup (servers.json)

To add a remote MCP server with OAuth 2.1:

```json
{
  "servers": {
    "github": {
      "label": "GitHub MCP",
      "transport": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "auth": {"type": "oauth2"},
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
3. Jack opens your browser → complete the login → the callback is captured
   on `http://127.0.0.1:<port>/callback` → tokens stored in Keychain as
   `mcp.github.oauth` and `mcp.github.client`.
4. Watch `mcp_oauth` WS events for stages: `browser_open` → `waiting_callback`
   → `callback_received` → `mcp_status` state=`connected`.

### Manual smoke-test (live OAuth)

Requirements: a real remote MCP server that supports OAuth 2.1 (e.g. GitHub,
Slack hosted MCP, or a local test AS using `oauth2-proxy` / `dex`).

```
# Start daemon
make run &

# Add server via curl
curl -s -X POST http://127.0.0.1:7437/mcp/servers \
  -H 'Content-Type: application/json' \
  -d '{"id":"github","label":"GitHub MCP","transport":"http",
       "url":"https://api.githubcopilot.com/mcp/",
       "auth":{"type":"oauth2"},"egress":"network","enabled":false}'

# Enable
curl -s -X POST http://127.0.0.1:7437/mcp/servers/github/enable

# Start auth flow (browser will open)
curl -s -X POST http://127.0.0.1:7437/mcp/servers/github/auth/start

# Observe: browser opens, you complete login, terminal shows:
#   [mcp] mcp connected server=github transport=http tools=N

# Verify tools appeared
curl -s http://127.0.0.1:7437/mcp/servers/github/tools | python3 -m json.tool
```

### Fingerprint re-consent UX

When a tool's definition changes between connections, the server emits:
```json
{"type": "mcp_tool_changed", "server": "github", "tools": ["github__push_files"]}
```
The tool is blocked (gate returns failed ToolResult: "tool requires re-consent").
The UI should show a "tool changed — review" diff card with the old vs new description.
Re-approval is done by deleting the tool's entry from `~/.autobot/mcp/approved.json`
and reconnecting (the reconnect re-baselines the new fingerprint).

Future: a `POST /mcp/servers/{id}/tools/{tool}/reconsent` endpoint can automate this.

### Spawn consent UX

On first connect of a stdio server, if no spawn approval exists for its exact
`(command, args)`, Jack asks: "Allow Jack to launch this process? npx -y @mcp/fs /path".
Approval is written to `~/.autobot/mcp/approved.json` and not re-asked unless the
command/args change. Denial sets the server state to `"denied"` without spawning.
```

**Verification:**
```bash
make check
# No code changed in this task; just doc. make check must remain green.
```

---

## Integration test additions

`tests/integration/test_mcp_integration.py` (already exists) — add:

```python
# Test: KeychainTokenStorage round-trip with a fake runner (no real Keychain)
# Test: LoopbackCallbackServer — start, simulate a GET /callback?code=abc&state=xyz,
#       wait() returns ("abc", "xyz") within timeout
# Test: LoopbackCallbackServer — wait() raises asyncio.TimeoutError when no request arrives
#       (use a tiny timeout like 0.1s)
# Test: open_browser raises ValueError for "file://" scheme
# Test: open_browser calls webbrowser.open for "https://" (monkeypatch)
```

Run with: `uv run --extra mcp pytest tests/integration/ -v`

---

## Self-Review Checklist

Before considering Phase 6 done, verify every item:

- [ ] `make check` green (ruff + ruff-format + mypy strict + pytest) with NO extra
- [ ] `uv run --extra mcp pytest tests/unit/ tests/integration/ -v` green
- [ ] No token value appears in any log line (grep `~/.autobot/logs/autobot.log` for `access_token`)
- [ ] `approved.json` is mode 0600 (`ls -l ~/.autobot/mcp/approved.json`)
- [ ] `open_browser` rejects `file://`, `custom://`, bare strings without scheme (unit test)
- [ ] `LoopbackCallbackServer` binds to `127.0.0.1` not `0.0.0.0` (checked in implementation)
- [ ] The stdio path is byte-for-byte unchanged (run the existing `test_mcp_session.py` suite)
- [ ] `OAuthClientProvider` is constructed with the verified parameter names from Step 0
- [ ] `streamablehttp_client` 3-tuple is correctly unpacked (only `read, write` passed to `ClientSession`)
- [ ] Fingerprint-changed tools produce `mcp_tool_changed` event AND are absent from the registry
- [ ] Spawn-denied servers set `state = "denied"` AND do NOT enter the stdio context manager
- [ ] Design doc updated with OAuth setup steps, manual smoke-test, and re-consent UX
- [ ] Manual smoke-test performed with at least one real remote OAuth server (documented)
- [ ] Conventional Commits, no Co-Authored-By trailer, explicit git add paths only

---

## Assumptions & Open Questions

1. **`token_endpoint_auth_method="none"`** — assumes all OAuth2 MCP servers registered
   by Jack are public clients (PKCE, no client_secret). If a server requires
   `client_secret_basic`, add it as a config field (`auth.client_auth_method`) and
   thread it into `OAuthClientMetadata`. Mark as a future extension.

2. **`client_metadata_url`** — left as `None` (dynamic client registration via DCR).
   If a server supports `client_id_metadata_document_supported=true`, the user can
   configure `auth.client_metadata_url` in `servers.json`; `_build_oauth_provider`
   would pass it through. Not implemented in Phase 6 to keep scope bounded.

3. **Re-consent UX (rug-pull) is one-sided in Phase 6.** The Python side blocks the
   tool and emits an event; the UI renders whatever it can with the existing `mcp_tool_changed`
   event shape. A full diff card (old description vs new) requires the worker to persist
   the old tool description alongside the fingerprint — deferred to a follow-up issue.

4. **`approved.json` is world-readable from Python** (only `autobot.*` can write it,
   but any process can read it). The fingerprints it contains are hashes of public
   tool schemas (not sensitive). Spawn records contain command + args (which may include
   local paths). Mode 0600 is set; acceptable for the threat model.

5. **`McpManager.set_confirmer()`** is a setter rather than a constructor parameter
   to keep backward compatibility with tests that construct `McpManager` without a
   confirmer. The default (no confirmer) auto-approves spawns (existing behavior),
   so no existing test breaks.
