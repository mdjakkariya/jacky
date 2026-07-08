"""Daemon-backed slash-command handlers (fake deps; no daemon/git/TTY)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from autobot.cli.coder_commands import Deps, handle


def _text(renderable: Any) -> str:
    console = Console(record=True, width=100)
    console.print(renderable)
    return console.export_text()


def _base_deps(**over: Any) -> Deps:
    base = Deps(
        working_diff=lambda cwd: None,
        post_settings=lambda url, updates: {"ok": True, "applied": sorted(updates)},
        get_models=lambda url: [],
        coder_undo=lambda url: {"ok": True, "message": "Reverted"},
        coder_checkpoints=lambda url: [],
        list_sessions=lambda url: [],
        resume_session=lambda url, sid: {"ok": True},
        new_session=lambda url: {"ok": True},
        load_settings=lambda: SimpleNamespace(
            llm_provider="ollama", llm_model="qwen3:8b", anthropic_model="x", coding_autonomy="plan"
        ),
    )
    return replace(base, **over)


def test_unknown_name_returns_none() -> None:
    assert handle("/help", "", base_url="http://x", cwd="/x", deps=_base_deps()) is None


def test_diff_no_changes_message() -> None:
    out = handle("/diff", "", base_url="http://x", cwd="/x", deps=_base_deps())
    assert isinstance(out, str) and "No changes" in out


def test_diff_renders_when_present() -> None:
    deps = _base_deps(working_diff=lambda cwd: "diff --git a b\n-old\n+new\n")
    out = handle("/diff", "", base_url="http://x", cwd="/x", width=80, deps=deps)
    assert "new" in _text(out)


def test_model_switch_uses_provider_field() -> None:
    seen: dict[str, Any] = {}
    deps = _base_deps(post_settings=lambda url, updates: seen.update(updates) or {"ok": True})
    out = handle("/model", "llama3", base_url="http://x", cwd="/x", deps=deps)
    assert seen == {"llm_model": "llama3"} and "llama3" in str(out)


def test_model_switch_anthropic_field() -> None:
    seen: dict[str, Any] = {}
    deps = _base_deps(
        post_settings=lambda url, updates: seen.update(updates) or {"ok": True},
        load_settings=lambda: SimpleNamespace(
            llm_provider="anthropic",
            llm_model="x",
            anthropic_model="claude-sonnet-5",
            coding_autonomy="plan",
        ),
    )
    handle("/model", "claude-opus-4-8", base_url="http://x", cwd="/x", deps=deps)
    assert seen == {"anthropic_model": "claude-opus-4-8"}


def test_autonomy_rejects_bad_value_and_posts_nothing() -> None:
    seen: list[Any] = []

    def _record(url: str, updates: dict[str, Any]) -> dict[str, Any]:
        seen.append(updates)
        return {"ok": True}

    deps = _base_deps(post_settings=_record)
    out = handle("/autonomy", "yolo", base_url="http://x", cwd="/x", deps=deps)
    assert "yolo" in str(out) and seen == []


def test_autonomy_sets_valid_value() -> None:
    seen: dict[str, Any] = {}
    deps = _base_deps(post_settings=lambda url, updates: seen.update(updates) or {"ok": True})
    handle("/autonomy", "auto", base_url="http://x", cwd="/x", deps=deps)
    assert seen == {"coding_autonomy": "auto"}


def test_sessions_resume_calls_client() -> None:
    seen: dict[str, Any] = {}
    deps = _base_deps(resume_session=lambda url, sid: seen.update(id=sid) or {"ok": True})
    out = handle("/sessions", "resume abc", base_url="http://x", cwd="/x", deps=deps)
    assert seen == {"id": "abc"} and "abc" in str(out)


def test_sessions_list_renders_table() -> None:
    deps = _base_deps(
        list_sessions=lambda url: [{"id": "abcdef12", "model": "m", "cwd": "/x", "mtime": 0.0}]
    )
    out = handle("/sessions", "", base_url="http://x", cwd="/x", deps=deps)
    assert "abcdef12" in _text(out)


def test_new_session_calls_client() -> None:
    called: list[bool] = []

    def _record(url: str) -> dict[str, Any]:
        called.append(True)
        return {"ok": True}

    deps = _base_deps(new_session=_record)
    out = handle("/new", "", base_url="http://x", cwd="/x", deps=deps)
    assert called == [True] and "new session" in str(out).lower()


def test_undo_list_renders_checkpoints() -> None:
    deps = _base_deps(
        coder_checkpoints=lambda url: [
            {"ref": "refs/jack/checkpoints/0", "sha": "a", "label": "first"}
        ]
    )
    out = handle("/undo", "list", base_url="http://x", cwd="/x", deps=deps)
    assert "first" in _text(out)
