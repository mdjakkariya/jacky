"""Tests for the filesystem sandbox path-jail."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.tools.sandbox import Sandbox, SandboxError


def test_relative_path_resolves_inside_root(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path / "ws")
    resolved = sandbox.resolve("notes/today.txt")
    assert resolved == (sandbox.root / "notes/today.txt").resolve()
    assert sandbox.root in resolved.parents


def test_absolute_path_inside_root_is_allowed(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path / "ws")
    inside = sandbox.root / "a.txt"
    assert sandbox.resolve(inside) == inside.resolve()


def test_parent_traversal_is_rejected(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path / "ws")
    with pytest.raises(SandboxError):
        sandbox.resolve("../escape.txt")


def test_absolute_path_outside_root_is_rejected(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path / "ws")
    with pytest.raises(SandboxError):
        sandbox.resolve("/etc/passwd")


def test_root_is_created(tmp_path: Path) -> None:
    root = tmp_path / "made" / "here"
    sandbox = Sandbox(root)
    assert sandbox.root.is_dir()
