"""Tests for the repo-map pure core (value objects + renderer). No tree-sitter needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.repomap import (
    FileMap,
    Symbol,
    build_repo_map,
    extract_python,
    render_repo_map,
)


def _fm(path: str, *syms: tuple[str, str, int, str, int]) -> FileMap:
    return FileMap(path=path, symbols=tuple(Symbol(*s) for s in syms))


def test_render_groups_by_file_and_shows_signatures() -> None:
    fm = _fm(
        "pkg/a.py",
        ("Greeter", "class", 1, "class Greeter:", 0),
        ("hello", "def", 2, "    def hello(self, name):", 1),
    )
    out = render_repo_map([fm])
    assert "pkg/a.py" in out
    assert "class Greeter:" in out
    assert "def hello(self, name):" in out
    # the class line appears before its method
    assert out.index("class Greeter:") < out.index("def hello")


def test_render_orders_files_by_path() -> None:
    fm_b = _fm("b.py", ("b", "def", 1, "def b():", 0))
    fm_a = _fm("a.py", ("a", "def", 1, "def a():", 0))
    out = render_repo_map([fm_b, fm_a])
    assert out.index("a.py") < out.index("b.py")


def test_render_empty_is_friendly() -> None:
    assert "no" in render_repo_map([]).lower()


def test_render_skips_files_with_no_symbols() -> None:
    out = render_repo_map([_fm("empty.py"), _fm("x.py", ("f", "def", 1, "def f():", 0))])
    assert "empty.py" not in out
    assert "x.py" in out


def test_render_respects_char_budget() -> None:
    files = [_fm(f"f{i}.py", ("g", "def", 1, "def g():", 0)) for i in range(200)]
    out = render_repo_map(files, char_budget=300)
    assert len(out) <= 400  # budget + a short truncation note
    assert "more" in out.lower() or "truncat" in out.lower()


def test_extract_python_finds_classes_functions_methods() -> None:
    ts = pytest.importorskip("tree_sitter_language_pack")  # needs the optional `code` extra
    assert ts  # importorskip returns the module
    src = (
        b"import os\n\n\ndef top():\n    return 1\n\n\n"
        b"class C:\n    def m(self, x):\n        return x\n"
    )
    syms = extract_python(src)
    names = {(s.name, s.kind, s.depth) for s in syms}
    assert ("top", "def", 0) in names
    assert ("C", "class", 0) in names
    assert ("m", "def", 1) in names  # method nested under the class
    method = next(s for s in syms if s.name == "m")
    assert method.signature.strip().startswith("def m(self, x):")
    assert method.line == 9


class _FakeConfirmer:
    def __init__(self, grant: bool) -> None:
        self._grant = grant

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return self._grant

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default if self._grant else ""


def _broker(tmp_path: Path, *, grant: bool = True) -> AccessBroker:
    pol = AccessPolicy(store_path=tmp_path / "access.json", workspace_root=tmp_path / "ws")
    return AccessBroker(pol, _FakeConfirmer(grant))


def _fake_extractor(source: bytes) -> list[Symbol]:
    # trivial deterministic "parser": one symbol per line beginning with "def "
    out: list[Symbol] = []
    for i, ln in enumerate(source.decode().splitlines(), start=1):
        if ln.startswith("def "):
            out.append(Symbol(ln[4:].split("(")[0], "def", i, ln, 0))
    return out


def test_build_repo_map_scans_python_files(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def alpha():\n    pass\n")
    (tmp_path / "pkg" / "b.py").write_text("def beta():\n    pass\n")
    (tmp_path / "notes.txt").write_text("def not_code():\n")  # non-.py ignored
    out = build_repo_map(str(tmp_path), _broker(tmp_path), extractor=_fake_extractor)
    assert "a.py" in out and "alpha" in out
    assert "b.py" in out and "beta" in out
    assert "notes.txt" not in out


def test_build_repo_map_denied(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def x():\n    pass\n")
    out = build_repo_map(str(tmp_path), _broker(tmp_path, grant=False), extractor=_fake_extractor)
    assert "don't have access" in out.lower()


def test_build_repo_map_empty_tree(tmp_path: Path) -> None:
    out = build_repo_map(str(tmp_path), _broker(tmp_path), extractor=_fake_extractor)
    assert "no" in out.lower()  # "No Python files" / "No symbols"


def test_build_repo_map_uses_cache_on_second_call(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def x():\n    pass\n")
    calls: list[bytes] = []

    def counting(source: bytes) -> list[Symbol]:
        calls.append(source)
        return _fake_extractor(source)

    b = _broker(tmp_path)
    build_repo_map(str(tmp_path), b, extractor=counting)
    build_repo_map(str(tmp_path), b, extractor=counting)  # unchanged file → cached
    assert len(calls) == 1  # extractor invoked once across two builds
