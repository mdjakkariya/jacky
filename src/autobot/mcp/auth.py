"""Token injection helpers for MCP server connections.

This module is a pure-function layer with no I/O: the Keychain lookup is injected
as a callable, so unit tests run without touching a real Keychain and so different
secret backends can be substituted trivially. It must not import the ``mcp`` SDK —
auth logic is transport-agnostic and must remain importable without the opt-in extra.

Phase 3 supports ``auth_type="token"`` (static bot/API token) for **stdio** servers:
the token is injected as an environment variable in ``StdioServerParameters(env=...)``,
which is the MCP spec's sanctioned path for stdio credential passing. HTTP bearer-header
injection (also ``auth_type="token"``) is added in Phase 6 alongside OAuth2.

Phase 6 adds:

- :class:`KeychainTokenStorage` — async ``TokenStorage`` backed by the macOS Keychain.
- :func:`open_browser` — scheme-validated, non-shell URL opener.
- :class:`LoopbackCallbackServer` — one-shot loopback HTTP OAuth callback server.

The ``mcp`` SDK is lazy-imported inside ``KeychainTokenStorage`` methods so this
module stays importable without the ``mcp`` extra. Token values are **never** logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import webbrowser
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.mcp.config import McpServerConfig
    from autobot.secrets import Runner

_log = get_logger("mcp")


def stdio_env_for(
    cfg: McpServerConfig,
    get_secret: Callable[[str], str | None],
) -> dict[str, str] | None:
    """Build the ``env`` dict for a stdio ``StdioServerParameters``.

    Starts from a copy of ``cfg.env`` (the non-secret vars from ``servers.json``).
    When ``cfg.auth_type == "token"`` and both ``cfg.secret_ref`` and
    ``cfg.token_env`` are set, looks up the token via ``get_secret(cfg.secret_ref)``
    and, if non-``None``, adds ``env[cfg.token_env] = token``.

    Returns ``None`` when the resulting dict is empty — ``StdioServerParameters``
    treats ``env=None`` as "inherit the full parent environment", which is the right
    default for unauthenticated local servers and avoids accidentally stripping
    ``PATH`` / ``HOME`` from the subprocess.

    Args:
        cfg: The server's config descriptor (no secrets stored here).
        get_secret: A callable that returns the secret for a Keychain account name,
            or ``None`` if unset/unavailable. Injected so callers can swap the
            real Keychain for a fake in tests.

    Returns:
        A non-empty ``dict[str, str]`` ready for ``StdioServerParameters(env=...)``,
        or ``None`` to signal "inherit parent env".
    """
    env: dict[str, str] = dict(cfg.env)

    if cfg.auth_type == "token" and cfg.secret_ref is not None and cfg.token_env is not None:
        token = get_secret(cfg.secret_ref)
        if token is not None:
            env[cfg.token_env] = token

    return env if env else None


class KeychainTokenStorage:
    """Async TokenStorage backed by the macOS Keychain.

    Token values are serialized as JSON blobs stored under:

    - ``mcp.<server_id>.oauth``  → OAuthToken
    - ``mcp.<server_id>.client`` → OAuthClientInformationFull

    The ``mcp`` SDK is lazy-imported inside each method so importing this module
    does not require the ``mcp`` extra. Values are **never** logged — only the
    Keychain account names appear in log output.

    Args:
        server_id: The MCP server's config id (e.g. ``"github"``).
        runner: Optional Keychain runner for testing (defaults to subprocess).
        client_id: When set, this storage represents a **pre-registered** OAuth
            app. ``get_client_info()`` returns a constructed
            ``OAuthClientInformationFull`` directly (bypassing Keychain) so the
            SDK skips Dynamic Client Registration entirely. ``set_client_info()``
            becomes a no-op (the pre-registered identity must not be overwritten).
        client_secret: The OAuth client secret for the pre-registered app.
            Stored in the Keychain under ``mcp.<id>.client_secret`` by the caller;
            passed in here already resolved. **Never logged.**
        redirect_uri: The fixed redirect URI that was registered with the OAuth
            app (e.g. ``http://127.0.0.1:8975/callback``).
    """

    def __init__(
        self,
        server_id: str,
        runner: Runner | None = None,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
    ) -> None:
        self._token_key = f"mcp.{server_id}.oauth"
        self._client_key = f"mcp.{server_id}.client"
        self._runner = runner
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    async def get_tokens(self) -> object | None:
        """Return the stored OAuthToken, or None if absent/unparseable.

        Returns:
            An ``OAuthToken`` instance, or ``None`` when the Keychain has no
            entry or the stored JSON cannot be parsed.
        """
        from mcp.shared.auth import OAuthToken  # lazy — mcp extra may be absent

        from autobot.secrets import get_secret

        raw = get_secret(self._token_key, self._runner)
        if raw is None:
            _log.info("oauth token absent in keychain key=%s", self._token_key)
            return None
        try:
            tok = OAuthToken.model_validate(json.loads(raw))
        except Exception:
            _log.warning("oauth token in keychain unparseable key=%s", self._token_key)
            return None
        # Lifecycle seam: a present refresh_token + finite expires_in means the SDK
        # should refresh silently rather than re-authorize. No token VALUES are logged.
        _log.info(
            "oauth token loaded key=%s has_refresh=%s expires_in=%s",
            self._token_key,
            bool(getattr(tok, "refresh_token", None)),
            getattr(tok, "expires_in", None),
        )
        return cast(object, tok)

    async def set_tokens(self, tokens: object) -> None:
        """Persist an OAuthToken to the Keychain as JSON.

        The serialized value is written directly to the Keychain and is
        **never** logged or printed.

        Args:
            tokens: An ``OAuthToken`` (or any pydantic model with
                ``model_dump_json``).
        """
        from autobot.secrets import set_secret

        raw = tokens.model_dump_json()  # type: ignore[attr-defined]
        set_secret(self._token_key, raw, self._runner)
        # A persist after a "token loaded (expired)" means a refresh succeeded (silent,
        # no browser). If a browser redirect happens with no persist in between, the
        # refresh path failed. Value never logged — only presence of a refresh token.
        _log.info(
            "oauth token persisted key=%s has_refresh=%s",
            self._token_key,
            bool(getattr(tokens, "refresh_token", None)),
        )

    async def get_client_info(self) -> object | None:
        """Return the stored OAuthClientInformationFull, or None.

        When this storage was constructed with a ``client_id`` (pre-registered
        OAuth app), returns a freshly constructed ``OAuthClientInformationFull``
        directly — no Keychain read required, and the SDK will skip Dynamic
        Client Registration because ``context.client_info`` is already set.

        Returns:
            An ``OAuthClientInformationFull`` instance, or ``None`` when absent
            or unparseable.
        """
        if self._client_id is not None:
            from mcp.shared.auth import OAuthClientInformationFull  # lazy

            _log.info("oauth client pre-registered (skipping DCR) key=%s", self._client_key)
            # OAuthClientInformationFull.redirect_uris expects list[AnyUrl]; cast to Any
            # so mypy doesn't reject the plain string — pydantic coerces it at runtime.
            redirect_uris_any: Any = [self._redirect_uri]
            return cast(
                object,
                OAuthClientInformationFull(
                    redirect_uris=redirect_uris_any,
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                    token_endpoint_auth_method=(
                        "client_secret_post" if self._client_secret else "none"
                    ),
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    client_name="Jack",
                ),
            )

        from mcp.shared.auth import OAuthClientInformationFull  # lazy

        from autobot.secrets import get_secret

        raw = get_secret(self._client_key, self._runner)
        if raw is None:
            _log.info("oauth client info absent key=%s — will register new", self._client_key)
            return None
        try:
            info = OAuthClientInformationFull.model_validate(json.loads(raw))
        except Exception:
            _log.warning("oauth client info in keychain unparseable key=%s", self._client_key)
            return None
        _log.info("oauth client info loaded key=%s", self._client_key)
        return cast(object, info)

    async def set_client_info(self, info: object) -> None:
        """Persist OAuthClientInformationFull to the Keychain.

        When this storage was constructed with a ``client_id`` (pre-registered
        OAuth app), this is a **no-op** — the SDK calls ``set_client_info`` after
        Dynamic Client Registration, but for a pre-registered app we must not
        overwrite the configured identity.

        The serialized value is written directly to the Keychain and is
        **never** logged or printed.

        Args:
            info: An ``OAuthClientInformationFull`` (or any pydantic model with
                ``model_dump_json``).
        """
        if self._client_id is not None:
            return  # pre-registered client — never overwrite with DCR result

        from autobot.secrets import set_secret

        raw = info.model_dump_json()  # type: ignore[attr-defined]
        set_secret(self._client_key, raw, self._runner)


def open_browser(url: str) -> None:
    """Open ``url`` in the default browser using ``webbrowser.open`` (non-shell).

    Only ``http`` and ``https`` schemes are accepted. Any other scheme (e.g.
    ``file://``, ``javascript:``, custom URI schemes) raises ``ValueError`` to
    prevent a malicious OAuth server from redirecting the user's browser to an
    unintended location.

    Args:
        url: The authorization URL returned by the OAuth server.

    Raises:
        ValueError: If the URL scheme is not ``http`` or ``https``.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"open_browser: refusing non-http/https URL scheme={parsed.scheme!r}")
    webbrowser.open(url)


_LOOPBACK_TIMEOUT_S = 120.0  # 2 minutes; user must complete the browser flow

# Fixed loopback port for OAuth servers that require a PRE-REGISTERED redirect URI
# (no dynamic client registration). The user registers this exact URI with their app.
OAUTH_CALLBACK_PORT = 8975


def oauth_redirect_uri() -> str:
    """The fixed loopback redirect URI to register with a pre-registered OAuth app.

    Returns:
        The redirect URI string ``http://127.0.0.1:8975/callback`` that must be
        registered exactly as-is with the OAuth app (e.g. Slack, GitHub).
    """
    return f"http://127.0.0.1:{OAUTH_CALLBACK_PORT}/callback"


# The fixed-port callback server is process-wide single-use: only one OAuth flow can
# hold OAUTH_CALLBACK_PORT at a time. We track the active one so a new flow (e.g. a
# retry after an abandoned attempt) can reclaim the port instead of failing to bind.
_active_fixed_server: asyncio.AbstractServer | None = None


class LoopbackCallbackServer:
    """One-shot loopback HTTP server that captures the OAuth callback.

    Binds to ``127.0.0.1:0`` (OS-assigned port), serves exactly one GET
    request, parses ``code`` and ``state`` from the query string, then closes.
    Designed to be used as the ``callback_handler`` for ``OAuthClientProvider``.

    Usage::

        server = LoopbackCallbackServer()
        redirect_uri = await server.start()
        # pass redirect_uri to OAuthClientProvider's client_metadata.redirect_uris
        code, state = await server.wait()  # blocks until callback or timeout

    Args:
        timeout: Seconds to wait for the OAuth callback before raising
            ``asyncio.TimeoutError``. Defaults to 120 s.
        port: The TCP port to bind. ``0`` (the default) lets the OS pick a free
            ephemeral port (suitable for servers that support dynamic client
            registration). Pass a fixed port (e.g. ``OAUTH_CALLBACK_PORT``) when
            the server requires a pre-registered redirect URI.
    """

    def __init__(self, timeout: float = _LOOPBACK_TIMEOUT_S, port: int = 0) -> None:
        self._timeout = timeout
        self._fixed_port = port
        self._port: int | None = None
        self._result: asyncio.Future[tuple[str, str | None]] | None = None
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> str:
        """Bind the server socket and return the redirect URI.

        Returns:
            The redirect URI to register: ``http://127.0.0.1:<port>/callback``.
            When ``port=0`` was passed to the constructor, the OS assigns the
            actual port; when a fixed port was given it is used directly.
        """
        global _active_fixed_server
        loop = asyncio.get_running_loop()
        self._result = loop.create_future()
        if self._fixed_port and _active_fixed_server is not None:
            # Reclaim the fixed port from a previous, abandoned flow (worker shut
            # down mid-auth, or the browser flow was never completed). All OAuth
            # flows share the manager's single loop, so closing here is in-loop.
            with contextlib.suppress(Exception):  # already closing/closed — fine
                _active_fixed_server.close()
            _active_fixed_server = None
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self._fixed_port)
        if self._fixed_port:
            _active_fixed_server = self._server
        # Retrieve the actual bound port from the first socket (always correct,
        # even when a fixed port was requested and the OS chose the same value).
        sockets = self._server.sockets
        assert sockets, "asyncio.start_server returned no sockets"
        self._port = sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self._port}/callback"

    async def wait(self) -> tuple[str, str | None]:
        """Block until the callback arrives or the timeout expires.

        Returns:
            ``(auth_code, state)`` — ``state`` may be ``None`` if the server
            omits it.

        Raises:
            asyncio.TimeoutError: If no callback arrives within ``timeout``
                seconds.
        """
        global _active_fixed_server
        assert self._result is not None, "call start() before wait()"
        assert self._server is not None, "call start() before wait()"
        try:
            return await asyncio.wait_for(asyncio.shield(self._result), timeout=self._timeout)
        finally:
            self._server.close()
            await self._server.wait_closed()
            if _active_fixed_server is self._server:
                _active_fixed_server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Parse GET /callback?code=...&state=... and resolve the future."""
        from urllib.parse import parse_qs, urlparse

        code = ""
        state: str | None = None
        try:
            line = (await reader.readline()).decode()
            # e.g. "GET /callback?code=abc&state=xyz HTTP/1.1"
            path = line.split(" ")[1] if " " in line else ""
            params = parse_qs(urlparse(path).query)
            code = (params.get("code") or [""])[0]
            state_vals = params.get("state")
            state = state_vals[0] if state_vals else None
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n"
                b"Authorized. You may close this tab.\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

        if self._result is not None and not self._result.done():
            if code:
                self._result.set_result((code, state))
            else:
                self._result.set_exception(ValueError("OAuth callback missing 'code' parameter"))
