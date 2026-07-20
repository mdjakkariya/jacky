"""A minimal Language Server Protocol client for semantic code navigation.

Phase 1 of the LSP epic: a small, synchronous JSON-RPC client that speaks to a language
server over stdio, plus the pieces the ``symbol`` tool needs — go-to-definition and
find-references. It is deliberately tiny (one request in flight at a time; notifications
like ``publishDiagnostics`` are collected, not awaited) because the tool's request/response
pattern is simple.

Design principle (see the epic): the ``symbol`` tool is language-agnostic and **always falls
back** to the textual (grep) + polyglot (repo_map) approach when no server backs a language or
the server binary isn't installed — so LSP only ever *adds* precision, never removes a
capability. This module is the *server* backend; the fallback lives in ``symbol_nav.py``.

Layering, so the protocol is unit-tested without a real process:
  * :func:`frame_message` / :func:`read_message` — pure Content-Length framing.
  * :class:`Transport` — a dict-in/dict-out seam; :class:`StdioTransport` is the real one.
  * :class:`LspClient` — the request/response + lifecycle logic, transport-agnostic.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

_log = get_logger("coder")

_HEADER_SEP = b"\r\n\r\n"


def frame_message(message: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC ``message`` with its ``Content-Length`` header (LSP stdio framing)."""
    body = json.dumps(message).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + _HEADER_SEP + body


def read_message(stream: Any) -> dict[str, Any] | None:
    """Read one ``Content-Length``-framed JSON-RPC message from a binary ``stream``.

    Returns the decoded object, or ``None`` at end of stream / on a malformed frame. ``stream``
    is any object with ``readline()`` and ``read(n)`` returning ``bytes`` (a subprocess stdout).
    """
    length: int | None = None
    while True:
        line = stream.readline()
        if not line:  # EOF before a full header
            return None
        if line in (b"\r\n", b"\n"):  # blank line — headers end
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError:
                return None
    if length is None:
        return None
    body = stream.read(length)
    if len(body) < length:
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


class Transport(Protocol):
    """A JSON-RPC message pipe: send a dict, receive the next dict (blocking), or ``None``."""

    def send(self, message: dict[str, Any]) -> None:
        """Send one JSON-RPC message."""
        ...

    def receive(self) -> dict[str, Any] | None:
        """Return the next message, or ``None`` at end of stream."""
        ...


class LspError(Exception):
    """A JSON-RPC error response from the language server."""


class LspClient:
    """Synchronous LSP client: correlate one request at a time; collect notifications.

    Not thread-safe and single-flight by design — the ``symbol`` tool issues one request,
    reads until the matching response id, and stashes any interleaved notifications (e.g.
    ``textDocument/publishDiagnostics``) in :attr:`diagnostics` for later use.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport
        self._id = 0
        self.diagnostics: list[dict[str, Any]] = []

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification (no response expected)."""
        self._t.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request and return its ``result``, reading past any interleaved notifications."""
        self._id += 1
        req_id = self._id
        self._t.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        while True:
            msg = self._t.receive()
            if msg is None:
                raise LspError(f"server closed before responding to {method!r}")
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise LspError(str(msg["error"]))
                return msg.get("result")
            if msg.get("method") == "textDocument/publishDiagnostics":
                self.diagnostics.append(msg.get("params", {}))
            # Any other notification / a server->client request we don't handle: ignore and
            # keep reading for our response.

    def initialize(self, root_uri: str) -> None:
        """Run the ``initialize`` handshake, then send ``initialized``."""
        self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {},
                "clientInfo": {"name": "jack", "version": "1"},
            },
        )
        self.notify("initialized", {})

    def did_open(self, uri: str, language_id: str, text: str, version: int = 1) -> None:
        """Tell the server a document is open (required before navigation requests)."""
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": version,
                    "text": text,
                }
            },
        )

    def definition(self, uri: str, line: int, character: int) -> list[dict[str, Any]]:
        """Return the definition location(s) for the symbol at 0-based (``line``, ``character``)."""
        result = self.request(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
        )
        return _as_locations(result)

    def references(
        self, uri: str, line: int, character: int, *, include_declaration: bool = True
    ) -> list[dict[str, Any]]:
        """Return the reference location(s) for the symbol at 0-based (``line``, ``character``)."""
        result = self.request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": include_declaration},
            },
        )
        return _as_locations(result)

    def shutdown(self) -> None:
        """Best-effort ``shutdown`` + ``exit`` (never raises)."""
        try:
            self.request("shutdown", None)
            self.notify("exit", None)
        except (LspError, OSError):
            _log.debug("lsp shutdown failed", exc_info=True)


def _as_locations(result: Any) -> list[dict[str, Any]]:
    """Normalise a Location | Location[] | LocationLink[] result into a list of Locations."""
    if result is None:
        return []
    items: Iterable[Any] = result if isinstance(result, list) else [result]
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "uri" in item and "range" in item:  # Location
            out.append({"uri": item["uri"], "range": item["range"]})
        elif "targetUri" in item:  # LocationLink
            out.append({"uri": item["targetUri"], "range": item.get("targetSelectionRange", {})})
    return out
