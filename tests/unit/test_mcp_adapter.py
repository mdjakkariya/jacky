"""Tests for the pure MCP adapters (no SDK, no network)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from autobot.core.types import Risk
from autobot.mcp import adapter


@dataclass
class FakeAnnotations:
    readOnlyHint: bool | None = None  # noqa: N815
    destructiveHint: bool | None = None  # noqa: N815
    idempotentHint: bool | None = None  # noqa: N815
    openWorldHint: bool | None = None  # noqa: N815


@dataclass
class FakeTool:
    name: str
    description: str | None = None
    inputSchema: dict[str, Any] = field(default_factory=dict)  # noqa: N815
    annotations: Any = None


@dataclass
class FakeBlock:
    type: str
    text: str = ""
    mimeType: str = ""  # noqa: N815
    uri: str = ""
    resource: Any = None


@dataclass
class FakeResult:
    content: Sequence[Any]
    isError: bool = False  # noqa: N815


def test_namespacing_roundtrip() -> None:
    assert adapter.namespaced("slack", "send_message") == "slack__send_message"
    assert adapter.split_namespaced("slack__send_message") == ("slack", "send_message")


def test_split_namespaced_rejects_unnamespaced() -> None:
    assert adapter.split_namespaced("plain") is None
    assert adapter.split_namespaced("__x") is None
    assert adapter.split_namespaced("x__") is None


def test_params_passthrough_and_empty_default() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    assert adapter.params_from_input_schema(schema) == schema
    assert adapter.params_from_input_schema(None) == {"type": "object", "properties": {}}
    assert adapter.params_from_input_schema({}) == {"type": "object", "properties": {}}


def test_result_joins_text_blocks() -> None:
    r = FakeResult(content=[FakeBlock("text", text="hello"), FakeBlock("text", text="world")])
    assert adapter.result_to_text(r) == ("hello\nworld", False)


def test_result_flags_error() -> None:
    r = FakeResult(content=[FakeBlock("text", text="boom")], isError=True)
    assert adapter.result_to_text(r) == ("boom", True)


def test_result_renders_non_text_placeholders() -> None:
    r = FakeResult(content=[FakeBlock("image", mimeType="image/png")])
    text, is_error = adapter.result_to_text(r)
    assert text == "[image image/png]"
    assert is_error is False


def test_result_empty_is_placeholder() -> None:
    assert adapter.result_to_text(FakeResult(content=[])) == ("(no content)", False)


def test_risk_override_wins() -> None:
    tool = FakeTool(name="send", annotations=FakeAnnotations(readOnlyHint=True))
    assert (
        adapter.risk_for(tool, floor=Risk.WRITE, overrides={"send": Risk.DESTRUCTIVE})
        is Risk.DESTRUCTIVE
    )


def test_risk_destructive_hint_maps_destructive() -> None:
    tool = FakeTool(name="rm", annotations=FakeAnnotations(destructiveHint=True))
    assert adapter.risk_for(tool, floor=Risk.WRITE, overrides={}) is Risk.DESTRUCTIVE


def test_risk_readonly_hint_maps_read_only() -> None:
    tool = FakeTool(name="search", annotations=FakeAnnotations(readOnlyHint=True))
    assert adapter.risk_for(tool, floor=Risk.WRITE, overrides={}) is Risk.READ_ONLY


def test_risk_no_hint_falls_to_floor() -> None:
    tool = FakeTool(name="post")  # no annotations
    assert adapter.risk_for(tool, floor=Risk.WRITE, overrides={}) is Risk.WRITE


def test_risk_from_name() -> None:
    assert adapter.risk_from_name("read") is Risk.READ_ONLY
    assert adapter.risk_from_name("write") is Risk.WRITE
    assert adapter.risk_from_name("destructive") is Risk.DESTRUCTIVE
    assert adapter.risk_from_name(None) is Risk.WRITE
    assert adapter.risk_from_name("nonsense") is Risk.WRITE


def test_fingerprint_is_stable_and_sensitive() -> None:
    a = FakeTool(name="t", description="d", inputSchema={"type": "object"})
    b = FakeTool(name="t", description="d", inputSchema={"type": "object"})
    c = FakeTool(name="t", description="CHANGED", inputSchema={"type": "object"})
    assert adapter.fingerprint(a) == adapter.fingerprint(b)
    assert adapter.fingerprint(a) != adapter.fingerprint(c)
