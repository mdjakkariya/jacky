"""Pure, conservative minification of an MCP tool's JSON-Schema parameters.

Verbose server schemas (Slack/GitHub) carry long nested ``description`` strings and
loose whitespace that cost tokens on every advertised round without helping a local
model call the tool. This trims them **near-losslessly**: it collapses whitespace and
drops ``description`` keys nested *below* the schema's top level, but never touches
``type``, ``required``, ``enum``, ``properties``, ``items``, ``default`` or any other
value the model needs. The tool's own top-level description is preserved (the model
relies on it to choose the tool). Pure and synchronous -- unit-tested without a runtime.
"""

from __future__ import annotations

import re
from typing import Any

_WS = re.compile(r"\s+")


def _collapse(text: str) -> str:
    """Collapse runs of whitespace to single spaces and strip the ends."""
    return _WS.sub(" ", text).strip()


def _walk(node: Any, *, top: bool) -> Any:
    """Recursively copy ``node``, collapsing strings and dropping nested descriptions."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            # Drop only *nested* descriptions; keep the schema's top-level one.
            if key == "description" and not top:
                continue
            out[key] = _walk(value, top=False)
        return out
    if isinstance(node, list):
        return [_walk(item, top=False) for item in node]
    if isinstance(node, str):
        return _collapse(node)
    return node


def minify_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a token-trimmed copy of an MCP parameters schema (conservative).

    Args:
        schema: A JSON-Schema object (a tool's ``parameters``).

    Returns:
        A new dict (the input is never mutated): whitespace collapsed in all string
        values; ``description`` keys below the top level removed. Structural and
        call-critical keys (``type``/``required``/``enum``/``properties``/...) are kept
        verbatim, so the advertised signature stays valid.
    """
    result = _walk(schema, top=True)
    assert isinstance(result, dict)  # top-level schema is always an object
    return result
