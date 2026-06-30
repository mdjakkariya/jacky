"""Tests for the pure, conservative MCP input-schema minifier (no model, no network)."""

from __future__ import annotations

from autobot.tools.schema_min import minify_schema

_VERBOSE = {
    "type": "object",
    "properties": {
        "channel": {
            "type": "string",
            "description": "The channel    ID   to post to.\n\n  Use the public ID.",
            "enum": ["general", "random"],
        },
        "count": {"type": "integer", "default": 1, "required": True},
    },
    "required": ["channel"],
}


def test_preserves_type_required_enum_and_structure() -> None:
    out = minify_schema(_VERBOSE)
    assert out["type"] == "object"
    assert out["required"] == ["channel"]
    assert out["properties"]["channel"]["type"] == "string"
    assert out["properties"]["channel"]["enum"] == ["general", "random"]
    assert out["properties"]["count"]["type"] == "integer"
    assert out["properties"]["count"]["default"] == 1
    assert out["properties"]["count"]["required"] is True


def test_drops_nested_descriptions_only() -> None:
    out = minify_schema(_VERBOSE)
    assert "description" not in out["properties"]["channel"]  # nested -> dropped


def test_keeps_top_level_description() -> None:
    out = minify_schema({"type": "object", "description": "Top  level.", "properties": {}})
    assert out["description"] == "Top level."  # top-level kept (whitespace collapsed)


def test_is_pure_does_not_mutate_input() -> None:
    import copy

    original = copy.deepcopy(_VERBOSE)
    minify_schema(_VERBOSE)
    assert original == _VERBOSE  # input untouched (deep copy inside)


def test_shrinks_serialized_size() -> None:
    import json

    before = len(json.dumps(_VERBOSE))
    after = len(json.dumps(minify_schema(_VERBOSE)))
    assert after < before  # net token win
