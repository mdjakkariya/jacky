"""The ``jack config`` command shell (I/O with injected fakes — no daemon/keyring)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, cast

import pytest

from autobot.cli import main as cli_main
from autobot.cli.config_cmd import Deps, run
from autobot.config import read_settings


def _deps(tmp: Path, *, up: bool = False, notified: list[dict[str, Any]] | None = None) -> Deps:
    sink: list[dict[str, Any]] = notified if notified is not None else []

    def notify_settings(base_url: str, updates: dict[str, Any]) -> dict[str, Any]:
        sink.append(updates)
        return {"ok": True}

    return Deps(
        settings_path=tmp / "settings.json",
        global_path=tmp / "settings.json",  # reads merge from here (same file as the write target)
        base_url="http://x",
        is_up=lambda _b: up,
        notify_settings=notify_settings,
        notify_secret=lambda b, n, v: {"ok": True},
        set_secret=lambda n, v: True,
        delete_secret=lambda n: True,
        get_secret=lambda n: None,
        prompt_secret=lambda prompt: "",
        launch_editor=lambda path: 0,
        out=io.StringIO(),
        err=io.StringIO(),
    )


def _out(d: Deps) -> str:
    return cast("io.StringIO", d.out).getvalue()


def _err(d: Deps) -> str:
    return cast("io.StringIO", d.err).getvalue()


def test_set_creates_file_and_persists(tmp_path: Path) -> None:
    d = _deps(tmp_path)
    assert run("set", ["provider", "anthropic"], d) == 0
    assert read_settings(tmp_path / "settings.json") == {"llm_provider": "anthropic"}


def test_set_unknown_key_errors_and_writes_nothing(tmp_path: Path) -> None:
    d = _deps(tmp_path)
    assert run("set", ["bogus", "x"], d) == 1
    assert not (tmp_path / "settings.json").exists()
    assert "unknown setting" in _err(d).lower()


def test_set_refuses_when_file_is_malformed(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text("{ this is not json", encoding="utf-8")
    d = _deps(tmp_path)
    assert run("set", ["provider", "anthropic"], d) == 1
    assert p.read_text(encoding="utf-8") == "{ this is not json"  # not clobbered
    assert "not valid json" in _err(d).lower()


def test_set_notifies_daemon_when_up(tmp_path: Path) -> None:
    notified: list[dict[str, Any]] = []
    d = _deps(tmp_path, up=True, notified=notified)
    assert run("set", ["autonomy", "auto"], d) == 0
    assert notified == [{"coding_autonomy": "auto"}]


def test_get_reads_effective(tmp_path: Path) -> None:
    d = _deps(tmp_path)
    run("set", ["provider", "anthropic"], d)
    assert run("get", ["provider"], d) == 0
    assert "anthropic" in _out(d)


def test_path_prints_settings_path(tmp_path: Path) -> None:
    d = _deps(tmp_path)
    assert run("path", [], d) == 0
    assert str(tmp_path / "settings.json") in _out(d)


def test_show_prints_settings_and_masks_secrets(tmp_path: Path) -> None:
    d = _deps(tmp_path)
    run("set", ["provider", "anthropic"], d)
    assert run("show", [], d) == 0
    out = _out(d)
    assert "llm_provider = anthropic" in out
    assert "anthropic_api_key: unset" in out  # get_secret fake returns None


def test_set_key_stores_and_notifies(tmp_path: Path) -> None:
    stored: dict[str, str] = {}
    d = _deps(tmp_path, up=True)

    def store(name: str, value: str) -> bool:
        stored[name] = value
        return True

    d.set_secret = store
    d.prompt_secret = lambda prompt: "sk-test"
    d.notify_secret = lambda b, n, v: {"ok": True}
    assert run("set-key", ["anthropic"], d) == 0
    assert stored == {"anthropic_api_key": "sk-test"}


def test_set_key_unknown_provider_errors(tmp_path: Path) -> None:
    d = _deps(tmp_path)
    assert run("set-key", ["mistral"], d) == 1
    assert "provider" in _err(d).lower()


def test_set_key_empty_clears(tmp_path: Path) -> None:
    cleared: list[str] = []
    d = _deps(tmp_path)
    d.prompt_secret = lambda prompt: ""

    def clear(name: str) -> bool:
        cleared.append(name)
        return True

    d.delete_secret = clear
    assert run("set-key", ["openai"], d) == 0
    assert cleared == ["openai_api_key"]


def test_edit_launches_editor_then_ok(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    launched: list[str] = []

    def editor(path: str) -> int:
        launched.append(path)
        Path(path).write_text('{"llm_provider": "anthropic"}', encoding="utf-8")
        return 0

    d = _deps(tmp_path)
    d.launch_editor = editor
    assert run("edit", [], d) == 0
    assert launched == [str(p)]


def test_edit_warns_on_invalid_json_and_does_not_notify(tmp_path: Path) -> None:
    notified: list[dict[str, Any]] = []
    d = _deps(tmp_path, up=True, notified=notified)

    def bad_editor(path: str) -> int:
        Path(path).write_text("{bad", encoding="utf-8")
        return 0

    d.launch_editor = bad_editor
    assert run("edit", [], d) == 1
    assert "not valid json" in _err(d).lower()
    assert notified == []  # never reload an invalid file


def test_jack_config_path_routes(capsys: pytest.CaptureFixture[str]) -> None:
    # `jack config path` prints the settings path and exits 0 without touching a daemon.
    rc = cli_main(["config", "path"])
    out = capsys.readouterr().out
    assert rc == 0 and "settings.json" in out


def test_set_writes_workspace_by_default_and_seeds_jack_gitignore(tmp_path: Path) -> None:
    ws_settings = tmp_path / ".jack" / "settings.json"
    d = _deps(tmp_path)
    d.settings_path = ws_settings  # the CLI default write target is the workspace file
    assert run("set", ["autonomy", "auto"], d) == 0
    assert read_settings(ws_settings) == {"coding_autonomy": "auto"}
    assert (tmp_path / ".jack" / ".gitignore").read_text(encoding="utf-8") == "sessions/\n"


def test_show_merges_workspace_over_global(tmp_path: Path) -> None:
    from autobot.config import write_settings

    global_file = tmp_path / "settings.json"
    ws_settings = tmp_path / ".jack" / "settings.json"
    write_settings({"coding_autonomy": "plan", "llm_provider": "ollama"}, global_file)
    write_settings({"coding_autonomy": "auto"}, ws_settings)
    d = _deps(tmp_path)
    d.global_path = global_file
    d.workspace_settings = ws_settings
    assert run("show", [], d) == 0
    out = _out(d)
    assert "coding_autonomy = auto" in out  # workspace wins
    assert "llm_provider = ollama" in out  # global shows through
    assert "workspace overrides" in out and "coding_autonomy" in out
