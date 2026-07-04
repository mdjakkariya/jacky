"""Tests for the repo-map pure core (value objects + renderer). No tree-sitter needed."""

from __future__ import annotations

import pytest

from autobot.tools.code.repomap import FileMap, Symbol, extract_python, render_repo_map


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
