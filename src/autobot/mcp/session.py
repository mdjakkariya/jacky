"""One MCP server's connection lifecycle, driven on the manager's event loop.

The MCP SDK is asyncio/anyio with structured concurrency: a session's transport
context managers and all of its calls must run on ONE task on ONE loop. So each
server gets a long-lived worker coroutine (:meth:`McpServerWorker.run`) that
enters the transport + ``ClientSession`` context managers, initializes, lists and
registers tools, then serves commands from an :class:`asyncio.Queue` until
shutdown. Synchronous tool handlers submit a :class:`_Call` via the loop and block
on a :class:`concurrent.futures.Future`; MCP errors/timeouts are raised so
``ToolRegistry.dispatch`` turns them into failed ``ToolResult``s. The heavy ``mcp``
SDK is **lazy-imported inside methods**, so importing this module needs no extra.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autobot.logging_setup import get_logger
from autobot.mcp import adapter
from autobot.mcp.approvals import DEFAULT_APPROVALS_PATH, load_approvals, record_fingerprints
from autobot.mcp.auth import stdio_env_for
from autobot.secrets import get_secret as _get_secret
from autobot.tools.registry import ToolSpec
from autobot.tools.schema_min import minify_schema

if TYPE_CHECKING:
    from collections.abc import Callable

    from autobot.mcp.config import McpServerConfig
    from autobot.tools.permission import Confirmer
    from autobot.tools.registry import ToolRegistry

_log = get_logger("mcp")

# How long a synchronous handler waits for a tool result before giving up. A slow
# remote tool returns a failed ToolResult (timeout) rather than blocking a turn forever.
CALL_TIMEOUT_S = 30.0


def _root_cause(exc: BaseException) -> BaseException:
    """Unwrap nested ExceptionGroups (anyio TaskGroup wrapping) to the first leaf."""
    inner: BaseException = exc
    for _ in range(6):  # bounded unwrap of nested ExceptionGroups
        if isinstance(inner, BaseExceptionGroup) and inner.exceptions:
            inner = inner.exceptions[0]
        else:
            break
    return inner


def friendly_error(exc: BaseException) -> str:
    """Best-effort human-readable message from a worker failure.

    anyio wraps transport/auth failures in a TaskGroup ``ExceptionGroup``; unwrap it
    to the root cause and translate the common "server has no OAuth dynamic client
    registration" case (e.g. GitHub) into actionable guidance, since the raw message
    ("unhandled errors in a TaskGroup") is useless in the UI.

    Args:
        exc: The exception caught by the worker's run loop.

    Returns:
        A concise, user-facing error string.
    """
    inner = _root_cause(exc)
    if isinstance(inner, ModuleNotFoundError) and (inner.name or "").split(".")[0] == "mcp":
        # The `mcp` client SDK is an opt-in extra; a plain install can configure servers
        # it can't connect to. Point at the fix instead of the useless raw import error.
        return (
            "MCP support isn't installed in this build. Add it with "
            "`uv tool install --reinstall 'jacky[mcp]'` (or `uv sync --extra mcp` "
            "from a source checkout), then reconnect."
        )
    msg = str(inner).strip() or inner.__class__.__name__
    low = msg.lower()
    _dcr_markers = ("registration failed", "registrationerror", "dynamic client registration")
    if any(m in low for m in _dcr_markers):
        return (
            "This server doesn't support automatic OAuth sign-in (dynamic client "
            "registration). Use a personal access token instead."
        )
    return msg


async def _accept_json_on_token_request(request: Any) -> None:
    """Force ``Accept: application/json`` on OAuth token-exchange requests.

    Some authorization servers (notably GitHub's ``/login/oauth/access_token``) return
    ``application/x-www-form-urlencoded`` by default, which the MCP SDK can't parse — it
    expects JSON. OAuth token requests are POSTs whose form body carries ``grant_type=``;
    tagging only those leaves MCP protocol requests (JSON-RPC) untouched.

    Args:
        request: The outgoing ``httpx.Request`` (typed ``Any`` — httpx is lazy-imported).
    """
    if request.method != "POST":
        return
    try:
        body = request.content
    except Exception:  # streaming/unread body — never an OAuth token request
        return
    if b"grant_type=" in body:
        request.headers["accept"] = "application/json"


async def _log_token_endpoint_response(response: Any) -> None:
    """Log OAuth token-endpoint responses (URL + grant + status) — never secret values.

    Surfaces what the SDK keeps to its own (unwired) logger: whether a token refresh is
    actually attempted, to which endpoint, and its HTTP status. Only requests carrying
    ``grant_type=`` are token requests; a non-200 here explains a fallback to browser
    re-auth.
    """
    request = response.request
    if request.method != "POST":
        return
    try:
        body = request.content
    except Exception:  # streaming/unread body — never an OAuth token request
        return
    if b"grant_type=" not in body:
        return
    grant = "refresh_token" if b"grant_type=refresh_token" in body else "authorization_code"
    _log.info(
        "oauth token endpoint url=%s grant=%s status=%s",
        request.url,
        grant,
        response.status_code,
    )


def _oauth_http_client_factory(
    headers: dict[str, str] | None = None,
    timeout: Any = None,
    auth: Any = None,
) -> Any:
    """MCP httpx client factory that adds the Accept-JSON token-request hook.

    Mirrors the SDK's default factory (so all MCP client defaults are preserved) and
    appends :func:`_accept_json_on_token_request` (so GitHub-style form-encoded token
    responses parse) plus :func:`_log_token_endpoint_response` (so token refresh/exchange
    attempts and their status are visible for debugging).
    """
    from mcp.shared._httpx_utils import create_mcp_http_client

    client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
    client.event_hooks.setdefault("request", []).append(_accept_json_on_token_request)
    client.event_hooks.setdefault("response", []).append(_log_token_endpoint_response)
    return client


def _expire_loaded_token(context: Any) -> None:
    """Force a reloaded OAuth token that has a refresh token to look expired.

    The mcp SDK loads tokens from storage on init but does NOT restore their expiry, so
    ``is_token_valid()`` returns True for an actually-expired token. The SDK then sends
    the stale token, gets a 401, and runs the FULL browser re-auth — it never refreshes
    on a 401. Marking the loaded token expired makes the SDK take its silent pre-request
    refresh path on reconnect instead. If the refresh then fails, the SDK falls back to
    the browser flow exactly as before — so this only ever removes an unnecessary
    re-authentication, never adds one. A no-op when there's no refresh token.
    """
    tokens = getattr(context, "current_tokens", None)
    if tokens is not None and getattr(tokens, "refresh_token", None):
        # NOT 0.0: the SDK checks `not token_expiry_time or now <= token_expiry_time`, and
        # `not 0.0` is True (0.0 is falsy) — which marks the token valid-forever. Use a
        # small POSITIVE epoch time: truthy, and firmly in the past, so the token reads as
        # expired and the SDK takes its silent refresh path instead of a browser re-auth.
        context.token_expiry_time = 1.0


def tool_allowed(name: str, allow: tuple[str, ...], deny: tuple[str, ...]) -> bool:
    """Whether a tool name passes the server's allow/deny globs.

    A deny match always excludes. With a non-empty ``allow`` list, only names
    matching at least one allow glob are kept; an empty ``allow`` permits all.
    """
    if deny and any(fnmatch(name, pat) for pat in deny):
        return False
    if allow:
        return any(fnmatch(name, pat) for pat in allow)
    return True


@dataclass
class _Call:
    """A queued tool invocation awaiting a result on its future."""

    tool: str
    args: dict[str, Any]
    future: concurrent.futures.Future[str]


class McpServerWorker:
    """Owns one server's connection, tool registration, and call serialization."""

    def __init__(
        self,
        config: McpServerConfig,
        registry: ToolRegistry,
        *,
        loop: asyncio.AbstractEventLoop,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        approvals_path: str | Path = DEFAULT_APPROVALS_PATH,
        confirmer: Confirmer | None = None,
        consent: str = "confirmer",
    ) -> None:
        self._cfg = config
        self._registry = registry
        self._loop = loop
        self._on_event = on_event
        self._approvals_path = approvals_path
        self._confirmer = confirmer
        self._consent = consent
        self._queue: asyncio.Queue[_Call | str] | None = None
        self._registered: list[str] = []
        self._state = "disconnected"
        self._tool_count = 0
        # Full snapshot of the server's tool list (built before allow/deny filtering).
        # Lets the UI show disabled tools and their risk; the registry only gets the
        # filtered subset. GIL-safe reads via the all_tools() copy method.
        self._all_tools: list[dict[str, object]] = []

    @property
    def state(self) -> str:
        """Worker state.

        One of ``"disconnected"``, ``"connected"``, ``"error"``, ``"denied"``,
        ``"pending_consent"``.
        """
        return self._state

    @property
    def tool_count(self) -> int:
        """Number of tools currently registered from this server."""
        return self._tool_count

    def all_tools(self) -> list[dict[str, object]]:
        """Return a copy of the full pre-filter tool snapshot.

        Includes tools that are excluded by ``tool_deny`` / ``tool_allow`` (with
        ``enabled=False``), so the UI can show and toggle them. The list is rebuilt
        on every ``_sync_tools`` call (connect and ``tools/list_changed`` resync).

        Returns:
            A copy of ``_all_tools``; each item has keys
            ``{name, description, risk, network, enabled, pending_reconsent}``.
            ``pending_reconsent`` is ``True`` only for tools blocked due to a
            fingerprint change (rug-pull), ``False`` otherwise.
        """
        return list(self._all_tools)

    # --- synchronous entry points (called from the engine thread) ---

    def submit_call(self, tool: str, args: dict[str, Any]) -> str:
        """Run a tool call on the worker's loop and block for the result.

        Raises ``RuntimeError`` if the server is not connected or the call times
        out, and re-raises a tool's own error — ``ToolRegistry.dispatch`` converts
        any of these into a failed ``ToolResult`` (so nothing escapes dispatch).
        """
        if self._queue is None or self._state != "connected":
            raise RuntimeError(f"MCP server {self._cfg.id!r} is not connected")
        future: concurrent.futures.Future[str] = concurrent.futures.Future()
        call = _Call(tool=tool, args=args, future=future)
        self._loop.call_soon_threadsafe(self._enqueue, call)
        try:
            return future.result(timeout=CALL_TIMEOUT_S)
        except concurrent.futures.TimeoutError as exc:
            raise RuntimeError(
                f"MCP tool {tool!r} on {self._cfg.id!r} timed out after {CALL_TIMEOUT_S}s"
            ) from exc

    def request_shutdown(self) -> None:
        """Ask the worker coroutine to exit (thread-safe)."""
        if self._queue is not None:
            self._loop.call_soon_threadsafe(self._enqueue, "shutdown")

    def _enqueue(self, item: _Call | str) -> None:
        """Put an item on the queue from the loop thread (created lazily in run())."""
        if self._queue is not None:
            self._queue.put_nowait(item)

    # --- the worker coroutine (runs on the manager's loop) ---

    async def run(self) -> None:
        """Connect, register tools, serve calls until shutdown, then clean up.

        Never raises: any failure marks the server ``"error"`` and unregisters its
        tools, so a bad server can't crash the loop or a turn.
        """
        self._queue = asyncio.Queue()
        try:
            if self._cfg.transport == "http":
                await self._run_http()
            else:
                if not await self._check_spawn_consent():
                    if self._consent == "explicit":
                        # Parked: consent is granted later via the API (grant_consent),
                        # which reconnects this server. Nothing was spawned.
                        self._state = "pending_consent"
                        _log.info("mcp spawn pending consent server=%s", self._cfg.id)
                        self._emit_status()
                    else:
                        self._state = "denied"
                        _log.info("mcp spawn denied by user server=%s", self._cfg.id)
                        self._emit_status(error="spawn denied by user")
                    return
                await self._run_stdio()
        except Exception as exc:  # never let the worker crash the loop
            self._state = "error"
            if isinstance(_root_cause(exc), TimeoutError):
                # OAuth browser sign-in wasn't completed in time — expected (the user
                # closed/ignored the tab), not a bug. Log a clean line instead of dumping
                # an alarming anyio ExceptionGroup traceback.
                _log.warning("mcp oauth sign-in not completed server=%s (timed out)", self._cfg.id)
                self._emit_status(error="sign-in not completed (timed out)")
            else:
                _log.exception("mcp worker failed server=%s", self._cfg.id)
                self._emit_status(error=friendly_error(exc))
        finally:
            # Flip off "connected" first so new submit_call()s are rejected fast,
            # then fail any calls still queued/in-flight so their callers don't have
            # to wait out the full CALL_TIMEOUT_S on a shutdown or crash.
            if self._state == "connected":
                self._state = "disconnected"
            self._fail_pending()
            self._unregister_all()
            self._tool_count = 0
            self._all_tools = []  # don't serve a stale tool snapshot after disconnect
            self._emit_status()
            _log.info("mcp disconnected server=%s", self._cfg.id)

    async def _check_spawn_consent(self) -> bool:
        """Return True if spawn is approved; False if denied.

        Checks approved.json first; if not approved, asks via confirmer (if wired).
        Falls back to True (auto-allow) when no confirmer is provided (non-interactive
        use, tests, or when consent was already granted).

        IMPORTANT: ``confirmer.confirm`` is a BLOCKING call (it waits for the user via
        the card/voice). This method runs on the manager's event loop, so the confirm
        MUST run via ``run_in_executor`` — calling it inline would freeze the loop
        (and every other server's worker + the message handler) until the user answers.

        Returns:
            ``True`` if the spawn is permitted, ``False`` if denied by the user.
        """
        from autobot.mcp.approvals import load_approvals, record_spawn_approval

        af = load_approvals(self._approvals_path)
        existing = af.spawn_approvals.get(self._cfg.id)
        command = self._cfg.command or ""
        args = list(self._cfg.args)

        if existing is not None and existing.command == command and existing.args == args:
            return True  # previously approved — skip the prompt

        if self._consent == "explicit":
            # Explicit mode never prompts and never auto-approves: the CLI grants
            # consent through the manager's grant_consent (POST /mcp/.../consent).
            return False

        if self._confirmer is None:
            # No UI confirmer: auto-approve (headless / non-interactive).
            record_spawn_approval(self._cfg.id, command, args, self._approvals_path)
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
            record_spawn_approval(self._cfg.id, command, args, self._approvals_path)
        return approved

    async def _run_stdio(self) -> None:
        """Stdio transport branch (extracted from the original run() for clarity)."""
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
        """HTTP transport branch: OAuth, static bearer token, or unauthenticated."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        url = self._cfg.url or ""
        auth_type = self._cfg.auth_type

        if auth_type == "oauth":
            auth = await self._build_oauth_provider()
            # Custom client factory forces Accept: application/json on the token
            # exchange so GitHub's form-encoded token response parses (see the hook).
            cm = streamablehttp_client(
                url, auth=auth, httpx_client_factory=_oauth_http_client_factory
            )
        elif auth_type == "token":
            headers = self._http_headers()
            cm = streamablehttp_client(url, headers=headers)
        else:
            cm = streamablehttp_client(url)

        async with (
            cm as (read, write, _get_session_id),
            ClientSession(read, write, message_handler=self._on_message) as session,
        ):
            await session.initialize()
            await self._sync_tools(session)
            self._state = "connected"
            self._emit_status()
            _log.info(
                "mcp connected server=%s transport=http tools=%d",
                self._cfg.id,
                self._tool_count,
            )
            await self._serve(session)

    def _http_headers(self) -> dict[str, str]:
        """Return HTTP Authorization headers for token-based auth, or empty dict.

        Reads the bearer token from the Keychain via ``secret_ref``. Returns
        ``{"Authorization": "Bearer <token>"}`` when ``auth_type == "token"`` and
        the secret is present; otherwise returns ``{}``.

        Returns:
            A dict suitable for passing as ``headers=`` to ``streamablehttp_client``.
        """
        if self._cfg.auth_type != "token" or not self._cfg.secret_ref:
            return {}
        token = _get_secret(self._cfg.secret_ref)
        if token is None:
            return {}
        return {"Authorization": f"Bearer {token}"}

    async def _build_oauth_provider(self) -> Any:
        """Construct an OAuthClientProvider for this server.

        Chooses between two paths:

        - **Pre-registered path** (when ``cfg.client_id`` is set): binds a
          fixed loopback port (``OAUTH_CALLBACK_PORT``) and builds a
          :class:`KeychainTokenStorage` pre-populated with the configured
          ``client_id``, ``client_secret`` (from the Keychain), and
          ``redirect_uri``. The storage's ``get_client_info()`` returns the
          pre-registered ``OAuthClientInformationFull`` so the SDK skips Dynamic
          Client Registration entirely.
        - **DCR path** (no ``client_id``): binds an ephemeral OS-assigned port
          and creates a plain :class:`KeychainTokenStorage`. The SDK performs
          Dynamic Client Registration on first connect.

        The provider is returned to ``_run_http`` which passes it as ``auth=``
        to ``streamablehttp_client``.

        Returns:
            An ``OAuthClientProvider`` instance (an ``httpx.Auth`` subclass).
        """
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        from autobot.mcp.auth import KeychainTokenStorage, LoopbackCallbackServer, open_browser

        client_id = self._cfg.client_id

        # token_endpoint_auth_method typed as Any to satisfy OAuthClientMetadata's
        # Literal constraint without importing the mcp SDK at module level.
        token_endpoint_auth_method: Any
        if client_id is not None:
            from autobot.mcp.auth import OAUTH_CALLBACK_PORT

            # client_secret: Keychain wins; fall back to the build-embedded file.
            client_secret = _get_secret(f"mcp.{self._cfg.id}.client_secret")
            if not client_secret:
                from autobot.mcp.client_secrets import default_client_secret

                client_secret = default_client_secret(self._cfg.id)
            cb_server = LoopbackCallbackServer(port=OAUTH_CALLBACK_PORT)
            redirect_uri = await cb_server.start()
            storage: Any = KeychainTokenStorage(
                self._cfg.id,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
            )
            token_endpoint_auth_method = "client_secret_post" if client_secret else "none"
        else:
            cb_server = LoopbackCallbackServer()
            redirect_uri = await cb_server.start()
            storage = KeychainTokenStorage(self._cfg.id)
            token_endpoint_auth_method = "none"

        redirect_uri_any: Any = redirect_uri  # OAuthClientMetadata.redirect_uris wants AnyUrl
        # No explicit ``scope`` — the authorization server applies its default scope.
        # MCP servers that require a specific scope are not yet supported here.
        metadata = OAuthClientMetadata(
            redirect_uris=[redirect_uri_any],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name="Jack",
            token_endpoint_auth_method=token_endpoint_auth_method,
        )

        async def redirect_handler(url: str) -> None:
            _log.info("mcp oauth redirect server=%s", self._cfg.id)
            self._emit_oauth_stage("browser_open", url=url)
            open_browser(url)

        async def callback_handler() -> tuple[str, str | None]:
            self._emit_oauth_stage("waiting_callback")
            result = await cb_server.wait()
            self._emit_oauth_stage("callback_received")
            return result

        # The SDK loads tokens but doesn't restore their expiry, so it sends a stale token,
        # gets a 401, and re-authorizes via the BROWSER instead of refreshing. Wrap
        # _initialize so a reloaded token is marked expired -> the SDK takes its silent
        # refresh path on reconnect. Operate through an explicitly Any-typed handle so
        # this wrap doesn't depend on whether mypy resolves the SDK class concretely.
        provider: Any = OAuthClientProvider(
            server_url=self._cfg.url or "",
            client_metadata=metadata,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        _orig_initialize = provider._initialize

        async def _initialize_then_expire() -> None:
            await _orig_initialize()
            _expire_loaded_token(provider.context)
            _log.info(
                "oauth reload-init server=%s token_expiry=%s",
                self._cfg.id,
                getattr(provider.context, "token_expiry_time", "?"),
            )

        provider._initialize = _initialize_then_expire
        return provider

    def _emit_oauth_stage(self, stage: str, url: str | None = None) -> None:
        """Publish an mcp_oauth event for the UI (never raises).

        Args:
            stage: A string identifying the OAuth flow stage (e.g. ``"browser_open"``).
            url: The authorize URL, included only for ``browser_open`` so a UI can
                offer it as a fallback when the browser didn't open.
        """
        payload: dict[str, Any] = {
            "type": "mcp_oauth",
            "server": self._cfg.id,
            "stage": stage,
        }
        if url:
            payload["url"] = url
        self._emit_event(payload)

    def _emit_event(self, payload: dict[str, Any]) -> None:
        """Publish any structured event to the sink (never raises).

        Args:
            payload: The event dict to pass to the on_event sink.
        """
        if self._on_event is None:
            return
        try:
            self._on_event(payload)
        except Exception:  # a UI hiccup must never break the worker
            _log.debug("mcp on_event sink failed", exc_info=True)

    async def _serve(self, session: Any) -> None:
        """Process queued commands until a shutdown sentinel arrives."""
        assert self._queue is not None
        while True:
            cmd = await self._queue.get()
            if cmd == "shutdown":
                return
            if cmd == "resync":
                await self._sync_tools(session)
                continue
            if isinstance(cmd, _Call):
                await self._do_call(session, cmd)

    def _fail_pending(self) -> None:
        """Resolve every still-queued call with an error (called on worker exit).

        Without this, a call enqueued behind the shutdown sentinel (or in flight when
        the worker crashes) would never have its future set, forcing the blocked
        ``submit_call`` caller to wait the full ``CALL_TIMEOUT_S`` instead of failing
        fast. Drains the queue and fails each pending :class:`_Call`.
        """
        if self._queue is None:
            return
        exc = RuntimeError(f"MCP server {self._cfg.id!r} disconnected")
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, _Call) and not item.future.done():
                item.future.set_exception(exc)

    async def _do_call(self, session: Any, call: _Call) -> None:
        """Invoke one tool and resolve its future (text on ok, exception on error)."""
        try:
            result = await session.call_tool(call.tool, call.args)
            text, is_error = adapter.result_to_text(result)
            if is_error:
                call.future.set_exception(RuntimeError(text))
            else:
                call.future.set_result(text)
        except Exception as exc:  # surface to the waiting handler, don't crash the loop
            if not call.future.done():
                call.future.set_exception(exc)

    async def _sync_tools(self, session: Any) -> None:
        """List the server's tools and reconcile the registry (add/replace/remove).

        Fingerprint gating runs for every allowed tool:

        - Fingerprint unchanged vs approved.json → register normally.
        - Fingerprint changed but risk NOT elevated (benign edit / list reorder /
          server update) → auto-re-baseline and register; log it.
        - Fingerprint changed AND risk elevated (read-only → write/destructive — a
          genuine rug-pull) → block (do not register); mark ``pending_reconsent=True``
          in ``_all_tools``; emit ``mcp_tool_changed``.
        - New tool (not in approved.json) → auto-register and baseline its fingerprint
          and risk in approved.json.

        Denied tools are skipped before fingerprinting, exactly as before.
        """
        listed = await session.list_tools()
        floor = adapter.risk_from_name(self._cfg.default_risk)
        overrides = {
            name: adapter.risk_from_name(value)
            for name, value in self._cfg.tool_risk_overrides.items()
        }
        network = self._cfg.egress == "network"

        # Load approved fingerprints + risks for this server (rug-pull gating).
        approvals = load_approvals(self._approvals_path)
        approved_fps = approvals.fingerprints.get(self._cfg.id, {})
        approved_risks = approvals.risks.get(self._cfg.id, {})
        new_fps: dict[str, str] = {}
        new_risks: dict[str, str] = {}
        reconsent_names: list[str] = []
        reconsent_bare: set[str] = set()  # bare tool names for _all_tools lookup

        # Build desired dict AND collect fingerprint decisions in one pass.
        desired: dict[str, ToolSpec] = {}
        for tool in listed.tools:
            if not tool_allowed(tool.name, self._cfg.tool_allow, self._cfg.tool_deny):
                continue
            reg_name = adapter.namespaced(self._cfg.id, tool.name)
            fp = adapter.fingerprint(tool)
            risk = adapter.risk_for(tool, floor=floor, overrides=overrides)
            risk_name = risk.name.lower()
            if reg_name in approved_fps and approved_fps[reg_name] == fp:
                # Unchanged. Backfill the risk if this is a pre-``risks`` approved.json
                # entry, so a future change can be judged.
                if reg_name not in approved_risks:
                    new_risks[reg_name] = risk_name
            elif reg_name in approved_fps:
                # Definition changed since approval. Block ONLY if the change ELEVATES
                # risk (a genuine rug-pull, e.g. read-only -> write/destructive). A benign
                # edit at the same-or-lower risk is auto-re-baselined, so a legitimate
                # server update doesn't block the tool forever. An unknown old risk
                # (pre-``risks`` file) is treated as benign — the one-time migration.
                old_name = approved_risks.get(reg_name)
                old_risk = adapter.risk_from_name(old_name) if old_name else None
                if old_risk is not None and risk > old_risk:
                    reconsent_names.append(reg_name)
                    reconsent_bare.add(tool.name)
                    _log.warning(
                        "mcp tool risk elevated server=%s tool=%s %s->%s"
                        " — blocking pending re-consent",
                        self._cfg.id,
                        reg_name,
                        old_risk.name.lower(),
                        risk_name,
                    )
                    continue  # do NOT add to desired
                new_fps[reg_name] = fp
                new_risks[reg_name] = risk_name
                _log.info(
                    "mcp tool definition updated server=%s tool=%s — re-baselined (risk=%s)",
                    self._cfg.id,
                    reg_name,
                    risk_name,
                )
            else:
                # New tool: auto-approve; baseline its fingerprint + risk.
                new_fps[reg_name] = fp
                new_risks[reg_name] = risk_name

            desired[reg_name] = ToolSpec(
                name=reg_name,
                description=tool.description or "",
                parameters=minify_schema(adapter.params_from_input_schema(tool.inputSchema)),
                handler=self._make_handler(tool.name),
                risk=risk,
                network=network,
            )

        # Build the full snapshot (all tools, pre-filter) for the UI — AFTER fingerprint
        # decisions so pending_reconsent can be set correctly.
        self._all_tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "risk": adapter.risk_for(t, floor=floor, overrides=overrides).name.lower(),
                "network": network,
                "enabled": tool_allowed(t.name, self._cfg.tool_allow, self._cfg.tool_deny),
                "pending_reconsent": t.name in reconsent_bare,
            }
            for t in listed.tools
        ]

        # Reconcile registry.
        for name in list(self._registered):
            if name not in desired:
                self._registry.unregister(name)
        for _name, spec in desired.items():
            self._registry.register(spec, replace=True)
        self._registered = list(desired)
        self._tool_count = len(desired)

        # Persist baselined/re-baselined fingerprints + risks and emit events.
        if new_fps or new_risks:
            record_fingerprints(self._cfg.id, new_fps, self._approvals_path, risks=new_risks)
        if reconsent_names:
            self._emit_event(
                {
                    "type": "mcp_tool_changed",
                    "server": self._cfg.id,
                    "tools": reconsent_names,
                }
            )

        _log.info("mcp tools synced server=%s count=%d", self._cfg.id, self._tool_count)

    def _make_handler(self, bare_tool: str) -> Callable[..., str]:
        """Build a synchronous ToolSpec handler that routes through ``submit_call``."""

        def handler(**kwargs: Any) -> str:
            return self.submit_call(bare_tool, kwargs)

        return handler

    async def _on_message(self, message: Any) -> None:
        """SDK message hook: enqueue a resync when the server's tool list changes."""
        from mcp.types import ServerNotification, ToolListChangedNotification

        if isinstance(message, ServerNotification) and isinstance(
            message.root, ToolListChangedNotification
        ):
            self._enqueue("resync")

    def _unregister_all(self) -> None:
        """Remove every tool this worker registered."""
        for name in self._registered:
            self._registry.unregister(name)
        self._registered = []

    def _emit_status(self, error: str | None = None) -> None:
        """Publish an mcp_status event (never raises).

        Args:
            error: Optional error string to include when the server entered an
                error state. Omit for normal connect/disconnect transitions.
        """
        payload: dict[str, Any] = {
            "type": "mcp_status",
            "server": self._cfg.id,
            "state": self._state,
            "tool_count": self._tool_count,
        }
        if error:
            payload["error"] = error
        self._emit_event(payload)
