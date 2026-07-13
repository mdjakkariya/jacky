"""__version__ is authoritative and stays in lockstep with pyproject + bump_version."""

from __future__ import annotations

import tomllib
from pathlib import Path

from scripts.bump_version import _CLI_FILES, set_version

import autobot

_ROOT = Path(__file__).resolve().parents[2]


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert autobot.__version__ == pyproject["project"]["version"]


def test_bump_version_rewrites_the_package_init() -> None:
    # bump_version must know how to rewrite src/autobot/__init__.py so a release
    # bump can't leave __version__ stale (the bug this task fixes).
    rel = "src/autobot/__init__.py"
    assert rel in _CLI_FILES  # the engine version constant lives on the CLI track
    pattern, template = _CLI_FILES[rel]
    out = set_version('__version__ = "0.0.1"\n', pattern, template, "9.9.9")
    assert out == '__version__ = "9.9.9"\n'
