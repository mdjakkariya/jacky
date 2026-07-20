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

import hashlib
import json
import queue
import threading
import time
from typing import TYPE_CHECKING, Any, Protocol

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

_log = get_logger("coder")

_HEADER_SEP = b"\r\n\r\n"
_DEFAULT_REQUEST_TIMEOUT = 8.0  # seconds to wait for a response before falling back


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
    """A JSON-RPC message pipe: send a dict, receive the next within ``timeout``, or ``None``."""

    def send(self, message: dict[str, Any]) -> None:
        """Send one JSON-RPC message."""
        ...

    def receive(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Return the next message, or ``None`` on timeout / end of stream."""
        ...


class StdioTransport:  # pragma: no cover - real subprocess boundary
    """Frame LSP messages over a server subprocess (background reader; ``receive`` has a timeout).

    A hung or dead server can't block a request forever — a reader thread drains stdout into a
    queue that ``receive`` polls with a deadline.
    """

    def __init__(self, proc: Any) -> None:
        self._proc = proc
        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._read_loop, name="lsp-reader", daemon=True).start()

    def _read_loop(self) -> None:
        while True:
            msg = read_message(self._proc.stdout)
            self._q.put(msg)  # a ``None`` (EOF) is enqueued so a waiter unblocks, then stop
            if msg is None:
                return

    def send(self, message: dict[str, Any]) -> None:
        """Write a framed message to the server's stdin."""
        self._proc.stdin.write(frame_message(message))
        self._proc.stdin.flush()

    def receive(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Next message within ``timeout``s; ``None`` on timeout or when the server closed."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


class LspError(Exception):
    """A JSON-RPC error response from the language server."""


class LspClient:
    """Synchronous LSP client: correlate one request at a time; collect notifications.

    Not thread-safe and single-flight by design — the ``symbol`` tool issues one request,
    reads until the matching response id, and stashes any interleaved notifications (e.g.
    ``textDocument/publishDiagnostics``) in :attr:`diagnostics` for later use.
    """

    def __init__(self, transport: Transport, *, timeout: float = _DEFAULT_REQUEST_TIMEOUT) -> None:
        self._t = transport
        self._id = 0
        self._timeout = timeout
        self.diagnostics: list[dict[str, Any]] = []
        # uri -> (version, content-hash): lets ``sync`` skip re-sending an unchanged file.
        self._opened: dict[str, tuple[int, str]] = {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification (no response expected)."""
        self._t.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request and return its ``result``; raise :class:`LspError` on error/timeout.

        Reads past interleaved notifications (collecting ``publishDiagnostics``) to find the
        response by id, bounded by the client timeout so a hung server falls back rather than
        wedging the turn.
        """
        self._id += 1
        req_id = self._id
        self._t.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + self._timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            msg = self._t.receive(timeout=remaining)
            if msg is None:
                raise LspError(f"no response to {method!r} in {self._timeout}s (timeout or closed)")
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise LspError(str(msg["error"]))
                return msg.get("result")
            if msg.get("method") == "textDocument/publishDiagnostics":
                self.diagnostics.append(msg.get("params", {}))
            # Any other notification / a late response to an abandoned request: ignore and
            # keep reading (bounded by ``remaining``) for our response.

    def initialize(self, root_uri: str) -> None:
        """Run the ``initialize`` handshake (declaring the features we use), then notify."""
        self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {
                    "textDocument": {
                        "synchronization": {"dynamicRegistration": False},
                        "definition": {"dynamicRegistration": False},
                        "references": {"dynamicRegistration": False},
                        "hover": {"contentFormat": ["plaintext", "markdown"]},
                        "rename": {"dynamicRegistration": False},
                    },
                },
                "clientInfo": {"name": "jack", "version": "1"},
            },
        )
        self.notify("initialized", {})

    def sync(self, uri: str, language_id: str, text: str) -> None:
        """Open ``uri`` (first time) or send a change (content differs); no-op if unchanged.

        Deduplicates document sync so we don't re-upload a file's full text on every query —
        the server keeps its parsed copy between requests (a real cost/latency saver).
        """
        digest = hashlib.sha1(
            text.encode("utf-8", "replace")
        ).hexdigest()  # dedup key, not security
        opened = self._opened.get(uri)
        if opened is None:
            self._opened[uri] = (1, digest)
            doc = {"uri": uri, "languageId": language_id, "version": 1, "text": text}
            self.notify("textDocument/didOpen", {"textDocument": doc})
        elif opened[1] != digest:
            version = opened[0] + 1
            self._opened[uri] = (version, digest)
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
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

    def hover(self, uri: str, line: int, character: int) -> Any:
        """Return the raw hover result (type/signature/doc) at 0-based (``line``, ``character``)."""
        return self.request(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
        )

    def rename(self, uri: str, line: int, character: int, new_name: str) -> dict[str, Any]:
        """Return the ``WorkspaceEdit`` renaming the symbol at 0-based (``line``, ``character``)."""
        result = self.request(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
                "newName": new_name,
            },
        )
        return result if isinstance(result, dict) else {}

    def shutdown(self) -> None:
        """Best-effort ``shutdown`` + ``exit`` (never raises)."""
        try:
            self.request("shutdown", None)
            self.notify("exit", None)
        except (LspError, OSError):
            _log.debug("lsp shutdown failed", exc_info=True)


def workspace_edit_files(edit: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Extract ``{uri: [TextEdit]}`` from a ``WorkspaceEdit``.

    Handles both shapes (``changes`` and ``documentChanges``). Pure; ignores malformed entries.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    changes = edit.get("changes")
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            if isinstance(edits, list):
                out.setdefault(str(uri), []).extend(e for e in edits if isinstance(e, dict))
    for dc in edit.get("documentChanges") or []:
        if isinstance(dc, dict) and isinstance(dc.get("textDocument"), dict):
            uri = str(dc["textDocument"].get("uri", ""))
            edits = dc.get("edits")
            if uri and isinstance(edits, list):
                out.setdefault(uri, []).extend(e for e in edits if isinstance(e, dict))
    return out


def apply_text_edits(text: str, edits: list[dict[str, Any]]) -> str:
    """Apply LSP ``TextEdit``s (``range`` → ``newText``) to ``text``, end-first.

    Edits are applied from the end so earlier ones keep their offsets. Pure. Overlapping edits
    are undefined per spec and not guarded against.
    """
    line_starts = [0]
    for line in text.split("\n"):
        line_starts.append(line_starts[-1] + len(line) + 1)  # + the newline

    def offset(pos: dict[str, Any]) -> int:
        row = int(pos.get("line", 0))
        base = line_starts[row] if 0 <= row < len(line_starts) else len(text)
        return max(0, min(base + int(pos.get("character", 0)), len(text)))

    def start_key(e: dict[str, Any]) -> tuple[int, int]:
        s = e.get("range", {}).get("start", {})
        return int(s.get("line", 0)), int(s.get("character", 0))

    result = text
    for e in sorted(edits, key=start_key, reverse=True):
        rng = e.get("range", {})
        lo, hi = offset(rng.get("start", {})), offset(rng.get("end", {}))
        if lo > hi:
            lo, hi = hi, lo
        result = result[:lo] + str(e.get("newText", "")) + result[hi:]
    return result


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
