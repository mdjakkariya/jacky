"""Unit tests for the /mcp REPL handler (fake surface + fake client deps)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from autobot.cli.classify import Segment
from autobot.cli.mcp_repl import Deps, handle
from autobot.cli.prompt import Answer


class FakeSurface:
    """Records commits; answers asks from a script keyed by ask order."""

    def __init__(self, answers: list[Answer] | None = None) -> None:
        """Preload the ``answers`` returned by successive ``ask`` calls, in order."""
        self.committed: list[object] = []
        self.asked: list[Segment] = []
        self._answers = list(answers or [])

    def commit(self, renderable: Any) -> None:
        """Record a committed renderable."""
        self.committed.append(renderable)

    def commit_command(self, label: str, output: list[str], *, gated: bool) -> None:
        """Record a finished command's label (output/gated are unused by these tests)."""
        self.committed.append(label)

    def set_activity(self, text: str) -> None:
        """No-op: these tests don't assert on the live activity line."""

    def set_todos(self, todos: list[tuple[str, str]]) -> None:
        """No-op: these tests don't assert on the live checklist."""

    def clear_activity(self) -> None:
        """No-op: these tests don't assert on the live activity line."""

    async def ask(self, seg: Segment) -> Answer:
        """Record the gate and return the next preset answer (default: decline)."""
        self.asked.append(seg)
        return self._answers.pop(0) if self._answers else Answer("no")


def _deps(**over: Any) -> Deps:
    base: dict[str, Any] = {
        "list_servers": lambda b: {"ok": True, "servers": []},
        "list_tools": lambda b, s: {"ok": True, "tools": []},
        "enable_server": lambda b, s: {"ok": True},
        "disable_server": lambda b, s: {"ok": True},
        "grant_consent": lambda b, s: {
            "ok": True,
            "server": {"server": s, "state": "connected", "tool_count": 3},
        },
        "remove_server": lambda b, s: {"ok": True},
        "add_server": lambda b, d: {"ok": True},
        "set_tool": lambda b, s, t, **kw: {"ok": True},
        "auth_start": lambda b, s: {"ok": True, "started": True},
        "post_settings": lambda b, u: {"ok": True},
        "post_secret": lambda b, n, v: {"ok": True},
    }
    base.update(over)
    return Deps(**base)


def _run(coro: Coroutine[Any, Any, None]) -> None:
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_list_is_default_verb() -> None:
    calls: list[str] = []

    def _list_servers(b: str) -> dict[str, Any]:
        calls.append(b)
        return {"ok": True, "servers": []}

    deps = _deps(list_servers=_list_servers)
    surface = FakeSurface()
    _run(handle("", surface, base_url="http://b", deps=deps))
    assert calls == ["http://b"] and surface.committed


def test_enable_with_pending_consent_grants_on_yes() -> None:
    deps = _deps(
        enable_server=lambda b, s: {
            "ok": True,
            "pending_consent": True,
            "command": "npx",
            "args": ["-y", "srv"],
        },
    )
    surface = FakeSurface(answers=[Answer("yes")])
    _run(handle("enable s1", surface, base_url="http://b", deps=deps))
    joined = " ".join(str(c) for c in surface.committed)
    assert "npx -y srv" in joined and "connected" in joined
    assert surface.asked and surface.asked[0].kind == "pending"


def test_enable_consent_denied_stays_pending() -> None:
    granted: list[str] = []

    def _grant_consent(b: str, s: str) -> dict[str, Any]:
        granted.append(s)
        return {"ok": True}

    deps = _deps(
        enable_server=lambda b, s: {
            "ok": True,
            "pending_consent": True,
            "command": "npx",
            "args": [],
        },
        grant_consent=_grant_consent,
    )
    surface = FakeSurface(answers=[Answer("no")])
    _run(handle("enable s1", surface, base_url="http://b", deps=deps))
    assert granted == []
    assert "pending" in " ".join(str(c) for c in surface.committed)


def test_add_wizard_builds_stdio_descriptor() -> None:
    added: list[dict[str, Any]] = []

    def _add_server(b: str, d: dict[str, Any]) -> dict[str, Any]:
        added.append(d)
        return {"ok": True}

    deps = _deps(add_server=_add_server)
    surface = FakeSurface(
        answers=[
            Answer("refine", "postgres"),  # id
            Answer("refine", "stdio"),  # transport
            Answer("refine", "npx -y server-postgres pg://x"),  # command line
            Answer("refine", "write"),  # risk floor
            Answer("yes"),  # save?
        ]
    )
    _run(handle("add", surface, base_url="http://b", deps=deps))
    d = added[0]
    assert d["id"] == "postgres" and d["transport"] == "stdio"
    assert d["command"] == "npx" and d["args"] == ["-y", "server-postgres", "pg://x"]
    assert d["egress"] == "local" and d["enabled"] is False and d["default_risk"] == "write"


def test_add_wizard_http_asks_auth_and_sets_secret_ref() -> None:
    added: list[dict[str, Any]] = []

    def _add_server(b: str, d: dict[str, Any]) -> dict[str, Any]:
        added.append(d)
        return {"ok": True}

    deps = _deps(add_server=_add_server)
    surface = FakeSurface(
        answers=[
            Answer("refine", "notion"),
            Answer("refine", "http"),
            Answer("refine", "https://mcp.notion.com/mcp"),
            Answer("refine", "token"),  # auth type (http q4)
            Answer("yes"),
        ]
    )
    _run(handle("add", surface, base_url="http://b", deps=deps))
    d = added[0]
    assert d["url"] == "https://mcp.notion.com/mcp" and d["egress"] == "network"
    assert d["auth"] == {"type": "token"} and d["secret_ref"] == "mcp.notion.token"


def test_add_wizard_cancels_on_empty_answer() -> None:
    added: list[dict[str, Any]] = []

    def _add_server(b: str, d: dict[str, Any]) -> dict[str, Any]:
        added.append(d)
        return {"ok": True}

    deps = _deps(add_server=_add_server)
    surface = FakeSurface(answers=[Answer("reject")])
    _run(handle("add", surface, base_url="http://b", deps=deps))
    assert added == []


def test_auth_token_stores_secret_and_reconnects() -> None:
    stored: list[tuple[str, str]] = []
    enabled: list[str] = []

    def _post_secret(b: str, n: str, v: str) -> dict[str, Any]:
        stored.append((n, v))
        return {"ok": True}

    def _enable_server(b: str, s: str) -> dict[str, Any]:
        enabled.append(s)
        return {"ok": True}

    deps = _deps(post_secret=_post_secret, enable_server=_enable_server)
    surface = FakeSurface(answers=[Answer("refine", "tok-123")])
    _run(handle("auth notion token", surface, base_url="http://b", deps=deps))
    assert stored == [("mcp.notion.token", "tok-123")] and enabled == ["notion"]
    assert surface.asked[0].kind == "secret"


def test_on_off_flip_allow_mcp() -> None:
    updates: list[dict[str, Any]] = []

    def _post_settings(b: str, u: dict[str, Any]) -> dict[str, Any]:
        updates.append(u)
        return {"ok": True}

    deps = _deps(post_settings=_post_settings)
    surface = FakeSurface()
    _run(handle("off", surface, base_url="http://b", deps=deps))
    _run(handle("on", surface, base_url="http://b", deps=deps))
    assert updates == [{"allow_mcp": False}, {"allow_mcp": True}]


def test_tool_risk_and_toggle() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def _set_tool(b: str, s: str, t: str, **kw: Any) -> dict[str, Any]:
        calls.append((s, t, kw))
        return {"ok": True}

    deps = _deps(set_tool=_set_tool)
    surface = FakeSurface()
    _run(handle("tool gh delete_repo risk destructive", surface, base_url="http://b", deps=deps))
    _run(handle("tool gh delete_repo off", surface, base_url="http://b", deps=deps))
    assert calls[0] == ("gh", "delete_repo", {"risk": "destructive"})
    assert calls[1] == ("gh", "delete_repo", {"enabled": False})


def test_unknown_verb_shows_usage() -> None:
    surface = FakeSurface()
    _run(handle("frobnicate", surface, base_url="http://b", deps=_deps()))
    assert "usage" in " ".join(str(c) for c in surface.committed).lower()
