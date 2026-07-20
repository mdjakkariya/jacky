"""Tests for the code-editing tools (read/write/edit/multi_edit)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autobot.core.types import ErrorCategory, Risk
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.tools import (
    delete_file,
    edit_file,
    move_file,
    multi_edit,
    multi_patch,
    read_file,
    read_files,
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


def test_read_file_char_cap_gives_accurate_resume_offset(tmp_path: Path) -> None:
    # When the char cap truncates before the line window ends, the tail must point at the
    # exact next line so a follow-up read resumes with no gap/overlap (G20).
    import re

    f = tmp_path / "big.py"
    f.write_text("\n".join("x" * 200 + str(i) for i in range(2000)) + "\n")
    out = read_file(str(f), _broker(tmp_path))
    m = re.search(r"continue with offset (\d+)", out)
    assert m, "expected a resume-offset hint when the char cap truncates"
    resume = int(m.group(1))
    assert 1 < resume < 2000
    out2 = read_file(str(f), _broker(tmp_path), offset=resume)
    assert f"\n{resume}\t" in out2  # the resume read starts exactly at the promised line


def test_read_files_reads_several(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("aaa\n")
    (tmp_path / "b.py").write_text("bbb\n")
    out = read_files([str(tmp_path / "a.py"), str(tmp_path / "b.py")], _broker(tmp_path))
    assert "a.py" in out and "aaa" in out
    assert "b.py" in out and "bbb" in out


def test_read_files_shows_per_file_error_inline(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("aaa\n")
    out = read_files([str(tmp_path / "a.py"), str(tmp_path / "missing.py")], _broker(tmp_path))
    assert "aaa" in out  # the readable file still shown
    assert "no file" in out.lower()  # the missing one's error is inline, not a hard failure


def test_read_files_rejects_non_list(tmp_path: Path) -> None:
    out = read_files("a.py", _broker(tmp_path))  # type: ignore[arg-type]
    assert isinstance(out, str)
    assert "list" in out.lower()


def test_read_files_registered(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.get("read_files") is not None


def test_delete_file_removes_a_file(tmp_path: Path) -> None:
    f = tmp_path / "gone.py"
    f.write_text("bye\n")
    out = delete_file(str(f), _broker(tmp_path))
    assert not f.exists()
    assert "deleted" in out.lower()


def test_delete_file_refuses_a_folder(tmp_path: Path) -> None:
    d = tmp_path / "dir"
    d.mkdir()
    out = delete_file(str(d), _broker(tmp_path))
    assert d.exists()  # untouched — no recursive removal
    assert "folder" in out.lower()


def test_delete_file_missing_is_not_ok(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    res = reg.dispatch("delete_file", {"path": str(tmp_path / "nope.py")})
    assert res.ok is False and res.category == ErrorCategory.NOT_FOUND


def test_move_file_renames(tmp_path: Path) -> None:
    src = tmp_path / "a.py"
    src.write_text("data\n")
    dst = tmp_path / "b.py"
    out = move_file(str(src), str(dst), _broker(tmp_path))
    assert not src.exists() and dst.read_text() == "data\n"
    assert "moved" in out.lower()


def test_move_file_refuses_to_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "a.py"
    src.write_text("new\n")
    dst = tmp_path / "b.py"
    dst.write_text("keep\n")
    reg = _registry(tmp_path)
    res = reg.dispatch("move_file", {"source": str(src), "dest": str(dst)})
    assert res.ok is False and res.category == ErrorCategory.EXISTS
    assert src.exists() and dst.read_text() == "keep\n"  # both untouched


def test_delete_and_move_are_destructive(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.get("delete_file").risk == Risk.DESTRUCTIVE  # type: ignore[union-attr]
    assert reg.get("move_file").risk == Risk.DESTRUCTIVE  # type: ignore[union-attr]


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


def test_edit_file_streams_a_unified_diff(tmp_path: Path) -> None:
    from autobot.core.streaming import output_sink

    f = tmp_path / "m.py"
    f.write_text("x = 1\n", encoding="utf-8")
    lines: list[str] = []
    token = output_sink.set(lines.append)
    try:
        edit_file(str(f), "x = 1", "x = 2", _broker(tmp_path))
    finally:
        output_sink.reset(token)
    joined = "\n".join(lines)
    assert "-x = 1" in joined and "+x = 2" in joined  # streamed the change as a unified diff


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


def test_edit_missing_search_hints_to_read_first_when_unread(tmp_path: Path) -> None:
    # Editing a file the model hasn't read this session, with a search that misses, nudges
    # it to read first (G5 read-before-edit hint).
    f = tmp_path / "m.py"
    f.write_text("x = 1\n")
    out = edit_file(str(f), "zzz", "q", _broker(tmp_path))
    assert "read the file first" in out.lower()


def test_edit_missing_search_no_hint_after_reading(tmp_path: Path) -> None:
    # Once the file has been read this session, a later missed search does not nag to read.
    f = tmp_path / "m.py"
    f.write_text("x = 1\n")
    broker = _broker(tmp_path)  # same broker instance tracks the read
    read_file(str(f), broker)
    out = edit_file(str(f), "zzz", "q", broker)
    assert "read the file first" not in out.lower()
    assert "not found" in out.lower()  # still reports the miss, just without the read nudge


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


def test_multi_patch_applies_across_files(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    a.write_text("x = 1\n")
    b = tmp_path / "b.py"
    b.write_text("y = 2\n")
    files = [
        {"path": str(a), "edits": [{"find": "x = 1", "replace": "x = 9"}]},
        {"path": str(b), "edits": [{"find": "y = 2", "replace": "y = 8"}]},
    ]
    out = multi_patch(files, _broker(tmp_path))
    assert a.read_text() == "x = 9\n" and b.read_text() == "y = 8\n"
    assert "2 file" in out


def test_multi_patch_is_atomic_across_files(tmp_path: Path) -> None:
    # File 2's edit can't match; NEITHER file may change (validated before any write).
    a = tmp_path / "a.py"
    a.write_text("x = 1\n")
    b = tmp_path / "b.py"
    b.write_text("y = 2\n")
    files = [
        {"path": str(a), "edits": [{"find": "x = 1", "replace": "x = 9"}]},
        {"path": str(b), "edits": [{"find": "zzz", "replace": "q"}]},
    ]
    reg = _registry(tmp_path)
    res = reg.dispatch("multi_patch", {"files": files})
    assert res.ok is False
    assert a.read_text() == "x = 1\n" and b.read_text() == "y = 2\n"  # both untouched — atomic


def test_multi_patch_rejects_malformed(tmp_path: Path) -> None:
    out = multi_patch([{"path": str(tmp_path / "a.py")}], _broker(tmp_path))  # no "edits"
    assert isinstance(out, str) and "malformed" in out.lower()


def test_multi_patch_registered_as_write(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.get("multi_patch") is not None
    assert reg.get("multi_patch").risk == Risk.WRITE  # type: ignore[union-attr]


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


def test_dispatch_write_file_success_is_ok(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    res = reg.dispatch("write_file", {"path": str(tmp_path / "n.py"), "content": "x\n"})
    assert res.ok


def test_dispatch_multi_edit_success_is_ok(tmp_path: Path) -> None:
    f = tmp_path / "z.py"
    f.write_text("a = 1\n")
    reg = _registry(tmp_path)
    res = reg.dispatch(
        "multi_edit", {"path": str(f), "edits": [{"find": "a = 1", "replace": "a = 2"}]}
    )
    assert res.ok and f.read_text() == "a = 2\n"


def test_dispatch_reports_failures_as_not_ok(tmp_path: Path) -> None:
    # Every expected code-tool failure must surface as ok=False (not a success-looking
    # string), so the harness guards that key off `ok` can see it. Regression for G1.
    exists = tmp_path / "exists.py"
    exists.write_text("original\n")
    binary = tmp_path / "b.bin"
    binary.write_bytes(b"\x00\x01data")
    editable = tmp_path / "m.py"
    editable.write_text("x = 1\nx = 1\n")

    reg = _registry(tmp_path)
    cases: list[tuple[str, dict[str, Any]]] = [
        ("read_file", {"path": str(tmp_path / "nope.py")}),  # missing
        ("read_file", {"path": str(binary)}),  # binary
        ("read_file", {}),  # missing arg
        ("write_file", {"path": str(exists), "content": "clobber\n"}),  # already exists
        ("edit_file", {"path": str(tmp_path / "nope.py"), "find": "a", "replace": "b"}),  # missing
        ("edit_file", {"path": str(editable), "find": "x = 1", "replace": "x = 2"}),  # ambiguous
        ("edit_file", {"path": str(editable), "find": "", "replace": "y"}),  # empty find
        (
            "multi_edit",
            {"path": str(editable), "edits": [{"find": "zzz", "replace": "q"}]},
        ),  # no match
        ("multi_edit", {"path": str(editable), "edits": []}),  # no edits
        ("multi_edit", {"path": str(editable), "edits": [{"find": "x = 1"}]}),  # malformed
    ]
    for name, args in cases:
        res = reg.dispatch(name, args)
        assert res.ok is False, f"{name}({args}) should be ok=False"
        assert res.content, f"{name}({args}) should carry an error message"

    # The failing edits/writes must not have touched anything.
    assert exists.read_text() == "original\n"
    assert editable.read_text() == "x = 1\nx = 1\n"


def test_dispatch_failure_categories(tmp_path: Path) -> None:
    # The error taxonomy (G3) is carried through to the ToolResult so callers can branch
    # on cause without parsing the message.
    exists = tmp_path / "exists.py"
    exists.write_text("original\n")
    ambiguous = tmp_path / "m.py"
    ambiguous.write_text("x = 1\nx = 1\n")
    reg = _registry(tmp_path)
    checks: list[tuple[str, dict[str, Any], str]] = [
        ("read_file", {"path": str(tmp_path / "nope.py")}, ErrorCategory.NOT_FOUND),
        ("read_file", {}, ErrorCategory.INVALID),
        ("write_file", {"path": str(exists), "content": "y"}, ErrorCategory.EXISTS),
        (
            "edit_file",
            {"path": str(ambiguous), "find": "x = 1", "replace": "x = 2"},
            ErrorCategory.AMBIGUOUS,
        ),
        (
            "edit_file",
            {"path": str(tmp_path / "nope.py"), "find": "a", "replace": "b"},
            ErrorCategory.NOT_FOUND,
        ),
    ]
    for name, args, cat in checks:
        res = reg.dispatch(name, args)
        assert res.category == cat, f"{name}: {res.category!r} != {cat!r}"


def test_register_adds_nav_and_exec_tools(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    for name in ("glob", "grep", "list_dir", "run_command"):
        assert reg.get(name) is not None, name


def test_nav_exec_risk_levels(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.get("glob").risk == Risk.READ_ONLY  # type: ignore[union-attr]
    assert reg.get("grep").risk == Risk.READ_ONLY  # type: ignore[union-attr]
    assert reg.get("list_dir").risk == Risk.READ_ONLY  # type: ignore[union-attr]
    assert reg.get("run_command").risk == Risk.DESTRUCTIVE  # type: ignore[union-attr]


def test_nav_exec_handlers_are_no_arg_safe(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    for name in ("glob", "grep", "list_dir", "run_command"):
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


def test_register_code_tools_forwards_blocklist_to_run_command(tmp_path: Path) -> None:
    reg = ToolRegistry()
    register_code_tools(reg, _broker(tmp_path), blocklist=["npm publish"])
    spec = reg.get("run_command")
    assert spec is not None
    out = spec.handler(command="npm publish")
    assert "blocked" in out.lower()


def test_register_code_tools_default_lists_do_not_block_normal_commands(tmp_path: Path) -> None:
    reg = _registry(tmp_path)  # no allowlist/blocklist passed — defaults to None
    spec = reg.get("run_command")
    assert spec is not None
    out = spec.handler(command="echo hi")
    assert "blocked" not in out.lower()
