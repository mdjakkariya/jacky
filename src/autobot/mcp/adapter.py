"""Pure adapters: MCP tool/result shapes → autobot's tool vocabulary.

No MCP SDK import lives here. Inputs are described by minimal structural
``Protocol``s, so this module — and its tests — stay SDK-free and import-light,
matching the repo's "pure logic is unit-tested without the runtime" pattern. The
session worker (added later) passes the SDK's real ``Tool`` / ``CallToolResult``
objects, which satisfy these protocols structurally.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from autobot.core.types import Risk


class _ToolLike(Protocol):
    """Structural view of an MCP ``Tool`` as returned by ``list_tools()``."""

    name: str
    description: str | None
    inputSchema: dict[str, Any]  # noqa: N815
    annotations: Any  # an annotations object or None; duck-typed to avoid union friction


class _ResultLike(Protocol):
    """Structural view of an MCP ``CallToolResult``."""

    content: Sequence[Any]
    isError: bool  # noqa: N815


_RISK_BY_NAME: dict[str, Risk] = {
    "read": Risk.READ_ONLY,
    "read_only": Risk.READ_ONLY,
    "readonly": Risk.READ_ONLY,
    "write": Risk.WRITE,
    "destructive": Risk.DESTRUCTIVE,
    "danger": Risk.DESTRUCTIVE,
}


def namespaced(server_id: str, tool_name: str) -> str:
    """Return the registry name for a server's tool, e.g. ``slack__send_message``."""
    return f"{server_id}__{tool_name}"


def split_namespaced(name: str) -> tuple[str, str] | None:
    """Split ``<id>__<tool>`` into ``(id, tool)``; ``None`` if not namespaced."""
    server_id, sep, tool = name.partition("__")
    if not sep or not server_id or not tool:
        return None
    return server_id, tool


def params_from_input_schema(input_schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """Map an MCP ``inputSchema`` (already JSON Schema) to ``ToolSpec.parameters``.

    Returns an empty object schema when the server omits a schema, so an
    argument-less tool is still advertised with a valid signature.
    """
    if not input_schema:
        return {"type": "object", "properties": {}}
    return dict(input_schema)


def result_to_text(result: _ResultLike) -> tuple[str, bool]:
    """Flatten a ``CallToolResult``'s content blocks to ``(text, is_error)``.

    Non-text blocks render as short placeholders so a tool returning an
    image/resource still yields a usable string. ``is_error`` mirrors the result's
    ``isError`` flag — the caller turns it into a failed ``ToolResult`` rather than
    raising.
    """
    parts: list[str] = []
    for block in result.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(str(getattr(block, "text", "")))
        elif btype == "resource":
            res = getattr(block, "resource", None)
            text = getattr(res, "text", None)
            parts.append(str(text) if text is not None else f"[resource {getattr(res, 'uri', '')}]")
        elif btype == "resource_link":
            parts.append(f"[resource_link {getattr(block, 'uri', '')}]")
        elif btype in ("image", "audio"):
            parts.append(f"[{btype} {getattr(block, 'mimeType', '')}]")
        else:
            parts.append(f"[{btype}]")
    text = "\n".join(p for p in parts if p).strip()
    return (text or "(no content)", bool(result.isError))


def risk_for(tool: _ToolLike, *, floor: Risk, overrides: Mapping[str, Risk]) -> Risk:
    """Classify a tool's :class:`Risk`. **Server annotations are advisory only.**

    Precedence: an explicit per-tool ``overrides`` entry wins; else a destructive
    hint maps to ``DESTRUCTIVE``; else a read-only hint maps to ``READ_ONLY``; else
    the server's ``floor`` (its ``default_risk``, normally ``WRITE``). Hints are
    never trusted to lower risk below the floor except the explicit read-only case.
    """  # noqa: D415
    if tool.name in overrides:
        return overrides[tool.name]
    ann = tool.annotations
    if ann is not None and bool(getattr(ann, "destructiveHint", False)):
        return Risk.DESTRUCTIVE
    if ann is not None and bool(getattr(ann, "readOnlyHint", False)):
        return Risk.READ_ONLY
    return floor


def risk_from_name(name: str | None, default: Risk = Risk.WRITE) -> Risk:
    """Map a config risk string ("read"/"write"/"destructive") to :class:`Risk`."""
    if not name:
        return default
    return _RISK_BY_NAME.get(name.strip().lower(), default)


def fingerprint(tool: _ToolLike) -> str:
    """Return a stable SHA-256 over a tool's identity-defining fields.

    Covers name, description, input schema, and annotation hints — so a server that
    silently redefines an approved tool ("rug pull") yields a different fingerprint,
    which the manager uses to force re-consent.
    """
    ann = tool.annotations
    ann_dict = (
        None
        if ann is None
        else {
            "readOnlyHint": getattr(ann, "readOnlyHint", None),
            "destructiveHint": getattr(ann, "destructiveHint", None),
            "idempotentHint": getattr(ann, "idempotentHint", None),
            "openWorldHint": getattr(ann, "openWorldHint", None),
        }
    )
    payload = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.inputSchema,
        "annotations": ann_dict,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
