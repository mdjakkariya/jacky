"""``jack skills`` — manage ``SKILL.md`` skills from a plain shell.

Skills are pure filesystem objects (a handful of standard directories full of
``SKILL.md`` files), unlike MCP servers or the coder session — so, unlike
``jack mcp``, this command never spawns or talks to a daemon. It builds a
:class:`~autobot.skills.registry.SkillRegistry` (installed skills) or a
:class:`~autobot.skills.source.SkillSource` (remote registries) directly and
acts in-process.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import TextIO

from autobot.config import Settings
from autobot.logging_setup import get_logger
from autobot.skills.registry import SkillRegistry, default_skill_dirs
from autobot.skills.source import SkillSource, SkillSourceError

_log = get_logger("cli")

_USAGE = """usage: jack skills <verb>
  list                  installed skills (name + description)
  search <query...>     search configured registries for a skill
  add <name>            install a skill found by search
  remove <name>         delete an installed skill
  show <name>           print an installed skill's full body"""


def _user_skills_dir() -> Path:
    """Where ``jack skills add``/``remove`` install/delete skills."""
    return Path.home() / ".autobot" / "skills"


def _registry() -> SkillRegistry:
    """A fresh registry over the standard skill directories (project + user)."""
    return SkillRegistry(default_skill_dirs(Path.home(), Path.cwd()))


def _cmd_list(out: TextIO) -> int:
    specs = _registry().specs()
    if not specs:
        print("No skills installed.", file=out)
        return 0
    for spec in specs:
        print(f"{spec.name}  —  {spec.description}", file=out)
    return 0


def _cmd_search(query: list[str], out: TextIO, err: TextIO) -> int:
    if not query:
        print(_USAGE, file=err)
        return 2
    settings = Settings.load()
    if not settings.skill_registries:
        print("No skill registries configured (set skill_registries in settings).", file=out)
        return 0
    source = SkillSource(settings.skill_registries, Path(settings.skill_cache_dir).expanduser())
    try:
        hits = source.search(" ".join(query))
    except SkillSourceError as exc:
        print(str(exc), file=err)
        return 1
    if not hits:
        print("No matching skill found.", file=out)
        return 0
    for hit in hits:
        print(f"{hit.name}  —  {hit.description}  [{hit.repo}]", file=out)
    return 0


def _cmd_add(name: str, out: TextIO, err: TextIO) -> int:
    settings = Settings.load()
    source = SkillSource(settings.skill_registries, Path(settings.skill_cache_dir).expanduser())
    try:
        hits = source.search(name)
        match = next((hit for hit in hits if hit.name == name), None)
        if match is None:
            print(f"No skill named {name!r} found in the configured registries.", file=err)
            return 1
        source.install(match, _user_skills_dir())
    except SkillSourceError as exc:
        print(str(exc), file=err)
        return 1
    print(f"Installed {name} from {match.repo}.", file=out)
    return 0


def _cmd_remove(name: str, out: TextIO, err: TextIO) -> int:
    dest = _user_skills_dir() / name
    if not dest.exists():
        print(f"No installed skill named {name}.", file=err)
        return 1
    shutil.rmtree(dest)
    print(f"Removed {name}.", file=out)
    return 0


def _cmd_show(name: str, out: TextIO, err: TextIO) -> int:
    body = _registry().body(name)
    if body is None:
        print(f"No skill named {name}.", file=err)
        return 1
    print(body, file=out)
    return 0


def run(argv: list[str]) -> int:
    """Dispatch one ``jack skills`` invocation.

    Args:
        argv: Everything after ``jack skills`` (e.g. ``["search", "weather"]``).

    Returns:
        0 on success, 1 on a failed/not-found operation, 2 on a usage error.
    """
    out, err = sys.stdout, sys.stderr
    if not argv or argv[0] == "list":
        return _cmd_list(out)
    verb, rest = argv[0], argv[1:]
    _log.debug("jack skills verb=%s", verb)
    if verb in ("--help", "-h", "help"):
        print(_USAGE, file=out)
        return 0
    if verb == "search":
        return _cmd_search(rest, out, err)
    if verb == "add" and len(rest) == 1:
        return _cmd_add(rest[0], out, err)
    if verb == "remove" and len(rest) == 1:
        return _cmd_remove(rest[0], out, err)
    if verb == "show" and len(rest) == 1:
        return _cmd_show(rest[0], out, err)
    print(_USAGE, file=err)
    return 2
