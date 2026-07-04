"""Tests for the code-editing tools (read/write/edit/multi_edit)."""

from __future__ import annotations

from pathlib import Path

from autobot.core.types import Risk
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.tools import (
    edit_file,
    multi_edit,
    read_file,
    register_code_tools,
    write_file,
)
from autobot.tools.registry import ToolRegistry


class _FakeConfirmer:
    """Approves or declines every grant prompt."""

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


def test_read_file_numbers_lines(tmp_path: Path) -> None:
    f = tmp_path / "p" / "a.py"
    f.parent.mkdir()
    f.write_text("first\nsecond\nthird\n")
    out = read_file(str(f), _broker(tmp_path))
    assert "1\tfirst" in out
    assert "2\tsecond" in out
    assert "3\tthird" in out


def test_read_file_offset_and_limit(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("\n".join(f"l{i}" for i in range(1, 11)) + "\n")
    out = read_file(str(f), _broker(tmp_path), offset=3, limit=2)
    assert "3\tl3" in out and "4\tl4" in out
    assert "l2" not in out and "l5" not in out


def test_read_file_denied_when_not_granted(tmp_path: Path) -> None:
    f = tmp_path / "p" / "a.py"
    f.parent.mkdir()
    f.write_text("secret-ish")
    out = read_file(str(f), _broker(tmp_path, grant=False))
    assert "don't have access" in out.lower()


def test_read_file_rejects_binary(tmp_path: Path) -> None:
    f = tmp_path / "b.bin"
    f.write_bytes(b"\x00\x01\x02data")
    assert "binary" in read_file(str(f), _broker(tmp_path)).lower()


def test_read_file_missing(tmp_path: Path) -> None:
    out = read_file(str(tmp_path / "nope.py"), _broker(tmp_path))
    assert "no file" in out.lower()


def test_write_file_creates_new(tmp_path: Path) -> None:
    f = tmp_path / "p" / "new.py"
    f.parent.mkdir()
    out = write_file(str(f), "print('hi')\n", _broker(tmp_path))
    assert f.read_text() == "print('hi')\n"
    assert "wrote" in out.lower()


def test_write_file_refuses_to_overwrite(tmp_path: Path) -> None:
    f = tmp_path / "exists.py"
    f.write_text("original\n")
    out = write_file(str(f), "clobber\n", _broker(tmp_path))
    assert f.read_text() == "original\n"  # untouched
    assert "already exists" in out.lower()
    assert "edit_file" in out


def test_write_file_denied_when_not_granted(tmp_path: Path) -> None:
    f = tmp_path / "p" / "new.py"
    f.parent.mkdir()
    out = write_file(str(f), "x", _broker(tmp_path, grant=False))
    assert "don't have access" in out.lower()
    assert not f.exists()


def test_edit_file_applies_whitespace_tolerant_edit(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("def f():\n    return 1   \n")  # trailing spaces (invisible drift)
    out = edit_file(str(f), "    return 1\n", "    return 2\n", _broker(tmp_path))
    assert f.read_text() == "def f():\n    return 2\n"
    assert "edited" in out.lower()


def test_edit_file_reports_ambiguous_without_writing(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("x = 1\nx = 1\n")
    out = edit_file(str(f), "x = 1", "x = 2", _broker(tmp_path))
    assert f.read_text() == "x = 1\nx = 1\n"  # unchanged
    assert "unique" in out.lower()


def test_edit_file_replace_all(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("x = 1\nx = 1\n")
    out = edit_file(str(f), "x = 1", "x = 2", _broker(tmp_path), replace_all=True)
    assert f.read_text() == "x = 2\nx = 2\n"
    assert "edited" in out.lower()


def test_edit_file_missing_target(tmp_path: Path) -> None:
    out = edit_file(str(tmp_path / "nope.py"), "a", "b", _broker(tmp_path))
    assert "no file" in out.lower()


def test_edit_file_empty_find(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("data\n")
    out = edit_file(str(f), "", "x", _broker(tmp_path))
    assert f.read_text() == "data\n"
    assert "exact text" in out.lower()


def test_edit_file_identical_find_replace(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("keep = 1\n")
    out = edit_file(str(f), "keep = 1", "keep = 1", _broker(tmp_path))
    assert f.read_text() == "keep = 1\n"
    assert "identical" in out.lower()


def test_multi_edit_applies_all_in_order(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\nc = 3\n")
    edits = [{"find": "a = 1", "replace": "a = 9"}, {"find": "c = 3", "replace": "c = 7"}]
    out = multi_edit(str(f), edits, _broker(tmp_path))
    assert f.read_text() == "a = 9\nb = 2\nc = 7\n"
    assert "2" in out


def test_multi_edit_is_atomic_on_failure(tmp_path: Path) -> None:
    # Second edit can't match; the whole operation must write nothing.
    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\n")
    edits = [{"find": "a = 1", "replace": "a = 9"}, {"find": "zzz", "replace": "q"}]
    out = multi_edit(str(f), edits, _broker(tmp_path))
    assert f.read_text() == "a = 1\nb = 2\n"  # untouched — atomic
    assert "edit 2" in out.lower()


def test_multi_edit_rejects_cascade_substring(tmp_path: Path) -> None:
    # Edit 2's find ("foobar") is a substring of edit 1's replace — reject, write nothing.
    f = tmp_path / "m.py"
    f.write_text("foo\n")
    edits = [{"find": "foo", "replace": "foobar"}, {"find": "foobar", "replace": "baz"}]
    out = multi_edit(str(f), edits, _broker(tmp_path))
    assert f.read_text() == "foo\n"
    assert "earlier edit" in out.lower()


def test_multi_edit_rejects_empty_list(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("a = 1\n")
    out = multi_edit(str(f), [], _broker(tmp_path))
    assert f.read_text() == "a = 1\n"
    assert "no edits" in out.lower()


def test_multi_edit_tolerates_malformed_edits(tmp_path: Path) -> None:
    # A non-dict / missing-key entry must produce a message, never a crash.
    f = tmp_path / "m.py"
    f.write_text("a = 1\n")
    out = multi_edit(str(f), [{"find": "a = 1"}], _broker(tmp_path))  # no "replace"
    assert isinstance(out, str)
    assert f.read_text() == "a = 1\n"
    assert "malformed" in out.lower()


def test_multi_edit_rejects_non_list_edits(tmp_path: Path) -> None:
    # A truthy non-list `edits` (e.g. the model sends a scalar) must not raise.
    f = tmp_path / "m.py"
    f.write_text("a = 1\n")
    out = multi_edit(str(f), 5, _broker(tmp_path))  # type: ignore[arg-type]
    assert isinstance(out, str)
    assert f.read_text() == "a = 1\n"
    assert "list" in out.lower()


def _registry(tmp_path: Path) -> ToolRegistry:
    reg = ToolRegistry()
    register_code_tools(reg, _broker(tmp_path))
    return reg


def test_register_adds_all_four_tools(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    for name in ("read_file", "write_file", "edit_file", "multi_edit"):
        assert reg.get(name) is not None, name


def test_registered_risk_levels(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.get("read_file").risk == Risk.READ_ONLY  # type: ignore[union-attr]
    assert reg.get("write_file").risk == Risk.WRITE  # type: ignore[union-attr]
    assert reg.get("edit_file").risk == Risk.WRITE  # type: ignore[union-attr]
    assert reg.get("multi_edit").risk == Risk.WRITE  # type: ignore[union-attr]


def test_registered_tools_are_gated_not_core(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.get("read_file").core is False  # type: ignore[union-attr]
    assert reg.get("edit_file").core is False  # type: ignore[union-attr]


def test_handlers_are_no_arg_safe(tmp_path: Path) -> None:
    # Every handler called with no args must return a string, never raise TypeError.
    reg = _registry(tmp_path)
    for name in ("read_file", "write_file", "edit_file", "multi_edit"):
        spec = reg.get(name)
        assert spec is not None
        out = spec.handler()
        assert isinstance(out, str) and out


def test_dispatch_read_file_through_registry(tmp_path: Path) -> None:
    f = tmp_path / "z.py"
    f.write_text("only\n")
    reg = _registry(tmp_path)
    res = reg.dispatch("read_file", {"path": str(f)})
    assert res.ok
    assert "1\tonly" in res.content


def test_dispatch_edit_file_replace_all_through_registry(tmp_path: Path) -> None:
    f = tmp_path / "z.py"
    f.write_text("v = 1\nv = 1\n")
    reg = _registry(tmp_path)
    res = reg.dispatch(
        "edit_file", {"path": str(f), "find": "v = 1", "replace": "v = 2", "replace_all": True}
    )
    assert res.ok
    assert f.read_text() == "v = 2\nv = 2\n"


def test_register_adds_nav_and_exec_tools(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    for name in ("glob", "grep", "run_command"):
        assert reg.get(name) is not None, name


def test_nav_exec_risk_levels(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.get("glob").risk == Risk.READ_ONLY  # type: ignore[union-attr]
    assert reg.get("grep").risk == Risk.READ_ONLY  # type: ignore[union-attr]
    assert reg.get("run_command").risk == Risk.DESTRUCTIVE  # type: ignore[union-attr]


def test_nav_exec_handlers_are_no_arg_safe(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    for name in ("glob", "grep", "run_command"):
        spec = reg.get(name)
        assert spec is not None
        out = spec.handler()
        assert isinstance(out, str) and out


def test_register_adds_repo_map(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.get("repo_map") is not None


def test_repo_map_risk_and_no_arg_safe(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    spec = reg.get("repo_map")
    assert spec is not None
    assert spec.risk == Risk.READ_ONLY
    out = spec.handler()  # no args → must return a string, never raise
    assert isinstance(out, str) and out
