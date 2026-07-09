"""The ``jack config`` command shell: thin I/O around the pure ``config_ops`` core.

Persists changes directly to ``settings.json`` (offline-safe) and best-effort notifies a
running coder daemon so the change applies live. All external effects (file, keyring,
daemon, editor, prompt) are injected via :class:`Deps` so the dispatch is unit-tested
without a real daemon or keyring.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Any, TextIO

from autobot import secrets
from autobot.cli import client, config_ops
from autobot.config import DEFAULT_SETTINGS_PATH, Settings, read_settings, write_settings
from autobot.logging_setup import get_logger

_log = get_logger("app")


def _no_editor(path: str) -> int:  # overridden by the CLI with a real $EDITOR launcher
    """Fallback editor launcher: signals 'no editor available'."""
    return 1


@dataclass
class Deps:
    """Injectable I/O for the config commands (defaults wire the real helpers)."""

    settings_path: str | Path = DEFAULT_SETTINGS_PATH
    base_url: str = ""
    is_up: Callable[[str], bool] = client.is_daemon_up
    notify_settings: Callable[[str, dict[str, Any]], dict[str, Any]] = client.post_settings
    notify_secret: Callable[[str, str, str], dict[str, Any]] = client.post_secret
    set_secret: Callable[[str, str], bool] = secrets.set_secret
    delete_secret: Callable[[str], bool] = secrets.delete_secret
    get_secret: Callable[[str], str | None] = secrets.get_secret
    prompt_secret: Callable[[str], str] = getpass
    launch_editor: Callable[[str], int] = _no_editor
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)


def _malformed(path: Path) -> bool:
    """True when the file exists and is non-empty but doesn't parse to a dict."""
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return False
    return read_settings(path) == {}


def _daemon_up(deps: Deps) -> bool:
    """Whether a coder daemon is reachable (never spawns one)."""
    return bool(deps.base_url) and deps.is_up(deps.base_url)


def _current(deps: Deps) -> dict[str, Any]:
    """Effective settings for reads: the live daemon view if up, else the local file."""
    if _daemon_up(deps):
        data = client.get_settings(deps.base_url)
        if data:
            return data
    return Settings.load(deps.settings_path).to_dict()


def _cmd_set(args: list[str], deps: Deps) -> int:
    if len(args) != 2:
        print("usage: jack config set <key> <value>", file=deps.err)
        return 1
    name, raw = args
    path = Path(deps.settings_path).expanduser()
    if _malformed(path):
        print(f"{path} is not valid JSON; fix it or run `jack config edit`.", file=deps.err)
        return 1
    on_disk = read_settings(path)
    try:
        key, value = config_ops.prepare_set(name, raw, current=on_disk, defaults=Settings())
    except config_ops.ConfigError as exc:
        print(str(exc), file=deps.err)
        return 1
    write_settings({**on_disk, key: value}, path)
    if _daemon_up(deps):
        res = deps.notify_settings(deps.base_url, {key: value})
        if not res.get("ok", True):
            print(
                f"saved; live-reload failed ({res.get('error')}), applies next start.",
                file=deps.err,
            )
    print(f"{key} = {value}", file=deps.out)
    return 0


def _cmd_get(args: list[str], deps: Deps) -> int:
    if len(args) != 1:
        print("usage: jack config get <key>", file=deps.err)
        return 1
    eff = _current(deps)
    try:
        key = config_ops.resolve_key(args[0], provider=str(eff.get("llm_provider", "ollama")))
    except config_ops.ConfigError as exc:
        print(str(exc), file=deps.err)
        return 1
    print(eff.get(key, ""), file=deps.out)
    return 0


def _cmd_show(deps: Deps) -> int:
    path = Path(deps.settings_path).expanduser()
    if _malformed(path):
        print(f"warning: {path} is not valid JSON; showing defaults.", file=deps.err)
    eff = _current(deps)
    secret_status = eff.get("_secrets")
    if not isinstance(secret_status, dict):
        secret_status = {
            name: deps.get_secret(name) is not None for name in config_ops.KEY_SECRETS.values()
        }
    print(config_ops.format_settings(eff, secret_status), file=deps.out)
    return 0


def _cmd_path(deps: Deps) -> int:
    print(str(Path(deps.settings_path).expanduser()), file=deps.out)
    return 0


def _cmd_set_key(args: list[str], deps: Deps) -> int:
    if len(args) != 1 or args[0] not in config_ops.KEY_SECRETS:
        allowed = ", ".join(sorted(config_ops.KEY_SECRETS))
        print(f"usage: jack config set-key <provider>  (provider: {allowed})", file=deps.err)
        return 1
    account = config_ops.KEY_SECRETS[args[0]]
    value = deps.prompt_secret(f"{args[0]} API key (blank to clear): ")
    ok = deps.set_secret(account, value) if value else deps.delete_secret(account)
    if not ok:
        print("couldn't write to the keyring.", file=deps.err)
        return 1
    if value and _daemon_up(deps):
        deps.notify_secret(deps.base_url, account, value)  # daemon reloads with the new key
    print(f"{account}: {'set' if value else 'cleared'}", file=deps.out)
    return 0


def _cmd_edit(deps: Deps) -> int:
    path = Path(deps.settings_path).expanduser()
    if not path.exists():
        write_settings(Settings.load(path).to_dict(), path)  # seed with current effective settings
    code = deps.launch_editor(str(path))
    if code != 0:
        print(f"editor exited with status {code}; no changes applied.", file=deps.err)
        return 1
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"{path} is not valid JSON ({exc}); leaving it for you to fix.", file=deps.err)
        return 1
    if _daemon_up(deps):
        deps.notify_settings(deps.base_url, read_settings(path))  # push the edited file live
    return 0


def run(action: str, args: list[str], deps: Deps) -> int:
    """Dispatch a ``jack config`` action. Returns a process exit code."""
    if action == "show":
        return _cmd_show(deps)
    if action == "get":
        return _cmd_get(args, deps)
    if action == "set":
        return _cmd_set(args, deps)
    if action == "path":
        return _cmd_path(deps)
    if action == "set-key":
        return _cmd_set_key(args, deps)
    if action == "edit":
        return _cmd_edit(deps)
    print(f"unknown config action: {action}", file=deps.err)
    return 2
