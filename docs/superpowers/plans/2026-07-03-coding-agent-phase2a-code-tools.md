# Coding-agent Phase 2a — Code edit tools (#48) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `src/autobot/tools/code/` package with code-oriented edit tools — `read_file` (line-numbered), `write_file` (create-only), `edit_file` (search/replace with `replace_all`), `multi_edit` (sequential atomic edits) — path-jailed through the existing `AccessBroker`/`AccessPolicy`, risk-classified, and exposed via `register_code_tools(registry, broker)` for the future `coder` profile (wired in #53).

**Architecture:** A pure, I/O-free search/replace **engine** (`edits.py`) and a thin **tools layer** (`tools.py`) that resolves every path through `AccessBroker.ensure(...)` (reusing the workspace jail, folder grants, and audit log unchanged) and calls the engine. The matcher deliberately follows the **claude-code `FileEditTool` strategy** — proven at scale — rather than Aider-style indentation reflow: match **exactly** first, then a single **trailing-whitespace-tolerant** pass (the one drift that read-before-edit can't see, since trailing spaces are invisible in line-numbered output). It requires the match be **unique** unless `replace_all` is set, and returns claude-code's clear "not found / N matches / identical" messages so the model self-corrects. It does **not** re-indent or guess — an ambiguous or absent match changes nothing. No new dependencies (no tree-sitter, no ripgrep — those are #49/#50). This package imports nothing from the Phase 1 `agent/` code, so it lands independently of PR #60.

**Tech Stack:** Python ≥ 3.11, stdlib only. Existing `ToolRegistry`/`ToolSpec`/`Risk` (`autobot.core.types`, `autobot.tools.registry`) and `AccessBroker`/`AccessPolicy`/`AccessDeniedError` (`autobot.tools.access`). Tests: `pytest` with explicit fakes (no mocking framework).

## Global Constraints

Every task's requirements implicitly include this section. Values are copied verbatim from `CLAUDE.md` and the epic's Phase-1 HARD RULES.

- **Conventional Commits**; **NO Co-Authored-By / no AI-attribution trailer** (repo convention).
- **Stage EXPLICIT paths only** — never `git add -A` / `.` / `-u`.
- **`make check` green** (ruff + ruff-format + mypy **strict** + pytest) before a task is DONE. Run it; do not hand-format (`make format` owns formatting/import order).
- `from __future__ import annotations` in **every** module; full type hints (mypy strict — note `warn_unused_ignores=true`, so a `# type: ignore` must be genuinely needed); value objects are `@dataclass(frozen=True, slots=True)` with no business logic; **line length 100**; Google-style docstrings on public modules/classes/functions (tests exempt from `D`).
- **Tools return strings and never raise out of `dispatch`** — a handler catches expected failures (`AccessDeniedError`, `PermissionError`, `OSError`) and returns a plain-English message; the registry turns any unexpected exception into a failed `ToolResult`. Never let a bad tool crash the loop.
- **Path jail is mandatory:** every path a tool touches goes through `broker.ensure(path, write=...)`. Never open a path the broker didn't return. Reads use `write=False`; writes/edits use `write=True`.
- **Per-tool guidance lives in `ToolSpec.description`** (when to use it + which typed/spoken cues map to it), NOT in any global prompt. Handlers registered via `lambda` must be **no-arg-safe** (keyword defaults) so a call missing an argument returns a message instead of raising `TypeError`.
- **Logging:** module-level `_log = get_logger("coder")`; log seam events at INFO (`read_file`, `write_file`, `edit_file`, `multi_edit` — name + size/lines/detail), not per-line noise. `%`-style args, not f-strings.
- **English only.** Tests: `uv run pytest <path> -q`.

---

## File Structure

- `src/autobot/tools/code/__init__.py` — package marker + re-export (`register_code_tools`).
- `src/autobot/tools/code/edits.py` — **pure** engine: `ReplaceResult`, `apply_replace(content, find, replace, *, replace_all=False)`. No I/O, no imports from `tools.*`.
- `src/autobot/tools/code/tools.py` — gated handlers (`read_file`, `write_file`, `edit_file`, `multi_edit`) + `register_code_tools(registry, broker)`. Depends on `edits.py`, `access.py`, `registry.py`, `core.types`.
- `tests/unit/test_code_edits.py` — the matcher's exact/ambiguity/replace_all/drift matrix.
- `tests/unit/test_code_tools.py` — handlers (success/failure/jail), create-only, atomicity + substring guard, registration + risk.

Ownership boundary: `edits.py` decides *whether/where* text matches and returns edited content; `tools.py` decides *whether the path is allowed* and does the read/write. Keeping them apart is what lets the matcher be tested exhaustively with plain strings.

### Reference alignment (why this shape)

This mirrors claude-code's `FileEditTool`/`FileReadTool`/`FileWriteTool` (`/Users/mohamedjakkariyar/work/claude-code/src/tools/`) so behaviour matches a battle-tested coding agent and transfers across providers:

- **Exact-match + one whitespace pass, never reindent.** claude-code matches exactly (plus a curly-quote normalization we defer — see Notes) and relies on read-before-edit + good errors, not fuzzy indentation. We add exactly one safe extra pass: trailing-whitespace tolerance.
- **`replace_all` flag**, default false → require a unique match; on multiple matches, the error tells the model to add context or set `replace_all` (claude-code's exact behaviour).
- **Guards:** `old_string == new_string` → "no changes"; empty `find` is rejected (creating files is `write_file`'s job); in `multi_edit`, a later edit's `find` may not be a substring of an earlier edit's `replace` (claude-code's cascade guard).
- **Line format** `{n}\t{line}` — claude-code's compact line prefix; universal across providers (its default `{n}→{line}` arrow form is Claude-specific, so we use the tab form). `MAX_LINES = 2000`, `offset` 1-based — claude-code's defaults.

---

### Task 1: Search/replace engine (`edits.py`)

The crux, kept deliberately simple and exact per the reference. A pure function that applies one search/replace, tries an exact match then a trailing-whitespace-tolerant match, requires uniqueness unless `replace_all`, and **never guesses** — an ambiguous or absent match returns the original content unchanged with a clear reason.

**Files:**
- Create: `src/autobot/tools/code/__init__.py`
- Create: `src/autobot/tools/code/edits.py`
- Test: `tests/unit/test_code_edits.py`

**Interfaces:**
- Produces (Task 3 relies on these exact names/types):
  - `ReplaceResult(ok: bool, content: str, detail: str)` — frozen/slots dataclass.
  - `apply_replace(content: str, find: str, replace: str, *, replace_all: bool = False) -> ReplaceResult`. On success `ok=True` and `content` is the edited text; on failure `ok=False`, `content` is the **original** text unchanged, and `detail` explains (empty search / identical / not found / N matches).

- [ ] **Step 1: Create the empty package marker**

Create `src/autobot/tools/code/__init__.py`:

```python
"""Code-editing tools for the coder profile (path-jailed, OS-neutral)."""

from __future__ import annotations
```

- [ ] **Step 2: Write the failing tests for the engine**

Create `tests/unit/test_code_edits.py`:

```python
"""Tests for the pure search/replace engine (claude-code-aligned: exact + one WS pass)."""

from __future__ import annotations

from autobot.tools.code.edits import apply_replace


def test_exact_unique_replace() -> None:
    r = apply_replace("a = 1\nb = 2\n", "b = 2", "b = 3")
    assert r.ok
    assert r.content == "a = 1\nb = 3\n"
    assert "exact" in r.detail.lower()


def test_empty_search_is_rejected() -> None:
    r = apply_replace("x", "", "y")
    assert not r.ok
    assert r.content == "x"
    assert "empty" in r.detail.lower()


def test_identical_find_and_replace_rejected() -> None:
    r = apply_replace("x = 1\n", "x = 1", "x = 1")
    assert not r.ok
    assert r.content == "x = 1\n"
    assert "identical" in r.detail.lower()


def test_not_found_leaves_content_unchanged() -> None:
    r = apply_replace("a = 1\n", "zzz", "q")
    assert not r.ok
    assert r.content == "a = 1\n"
    assert "not found" in r.detail.lower()


def test_multiple_exact_is_ambiguous_by_default() -> None:
    r = apply_replace("x = 1\nx = 1\n", "x = 1", "x = 2")
    assert not r.ok
    assert r.content == "x = 1\nx = 1\n"  # nothing changed
    assert "unique" in r.detail.lower()


def test_replace_all_replaces_every_occurrence() -> None:
    r = apply_replace("x = 1\nx = 1\n", "x = 1", "x = 2", replace_all=True)
    assert r.ok
    assert r.content == "x = 2\nx = 2\n"


def test_trailing_whitespace_drift_matches() -> None:
    # The file has trailing spaces the model can't see in line-numbered output; still matches.
    content = "def f():\n    return 1   \n"
    r = apply_replace(content, "    return 1\n", "    return 2\n")
    assert r.ok
    assert r.content == "def f():\n    return 2\n"
    assert "whitespace" in r.detail.lower()


def test_multiline_exact_block_replace() -> None:
    r = apply_replace("start\nline1\nline2\nend\n", "line1\nline2", "only")
    assert r.ok
    assert r.content == "start\nonly\nend\n"


def test_no_trailing_newline_preserved() -> None:
    r = apply_replace("a\nb", "b", "c")  # file has no final newline
    assert r.ok
    assert r.content == "a\nc"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_edits.py -q`
Expected: FAIL — `ModuleNotFoundError` / `ImportError` for `autobot.tools.code.edits`.

- [ ] **Step 4: Implement the engine**

Create `src/autobot/tools/code/edits.py`:

```python
"""Search/replace engine for code edits (pure, no I/O).

The engine ``edit_file`` and ``multi_edit`` use to apply one search/replace block.
It follows claude-code's ``FileEditTool`` strategy — exact matching plus a single
tolerant pass — rather than fuzzy indentation reflow, because the model reads a file
(line-numbered) right before editing, so it already has the exact text:

1. **exact** — the search text appears verbatim; it must be unique unless
   ``replace_all`` is set, in which case every occurrence is replaced.
2. **trailing whitespace** — if the exact pass finds nothing, match line-by-line after
   ``rstrip()`` (the one drift read-before-edit can't reveal, since trailing spaces are
   invisible in numbered output); this pass requires a unique match.

Guards: an empty search, an identical search/replacement, no match, or a non-unique
match all leave the content unchanged and explain why — the engine never guesses where
an edit lands. All file/permission handling lives in ``tools.py``; this module is pure
so the matcher can be tested exhaustively with plain strings.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    """Outcome of one search/replace attempt."""

    ok: bool
    """True when the search text matched uniquely (or ``replace_all``); then ``content``
    is the edited text."""
    content: str
    """The edited content when ``ok``; the original content unchanged otherwise."""
    detail: str
    """Which pass matched (e.g. ``"exact match"``) or why it failed."""


def _lines(text: str) -> list[str]:
    r"""Split on ``\n`` so that ``"\n".join(_lines(t)) == t`` round-trips exactly."""
    return text.split("\n")


def _block_lines(text: str) -> list[str]:
    """A search/replacement block as lines, dropping one trailing empty from a final newline.

    So ``"    return 1\n"`` and ``"    return 1"`` describe the same single line — the
    search and replacement blocks are normalized the same way, keeping line counts aligned.
    """
    ls = _lines(text)
    if ls and ls[-1] == "":
        ls = ls[:-1]
    return ls


def _rstrip_hits(c_lines: list[str], f_keys: list[str]) -> list[int]:
    """Start indices where a window of ``c_lines`` equals ``f_keys`` after ``rstrip()``."""
    win = len(f_keys)
    if win == 0 or win > len(c_lines):
        return []
    return [
        i
        for i in range(0, len(c_lines) - win + 1)
        if [c_lines[j].rstrip() for j in range(i, i + win)] == f_keys
    ]


def apply_replace(
    content: str, find: str, replace: str, *, replace_all: bool = False
) -> ReplaceResult:
    """Apply one search/replace to ``content``.

    Returns a :class:`ReplaceResult`; on any failure the original ``content`` is returned
    unchanged so the caller can leave the file untouched.
    """
    if find == "":
        return ReplaceResult(False, content, "empty search text")
    if find == replace:
        return ReplaceResult(False, content, "the search and replacement text are identical")

    # Pass 1 — exact.
    n = content.count(find)
    if n >= 1:
        if n > 1 and not replace_all:
            return ReplaceResult(
                False,
                content,
                f"the search text appears {n} times; add surrounding context to make it "
                "unique, or set replace_all to replace every occurrence",
            )
        edited = content.replace(find, replace, -1 if replace_all else 1)
        return ReplaceResult(True, edited, f"exact match ({n})" if replace_all else "exact match")

    # Pass 2 — trailing-whitespace-tolerant, unique only.
    c_lines = _lines(content)
    f_keys = [ln.rstrip() for ln in _block_lines(find)]
    if f_keys:
        hits = _rstrip_hits(c_lines, f_keys)
        if len(hits) > 1:
            return ReplaceResult(
                False,
                content,
                f"the search text matches {len(hits)} places (ignoring trailing whitespace); "
                "add surrounding context to make it unique",
            )
        if len(hits) == 1:
            i = hits[0]
            new_lines = c_lines[:i] + _block_lines(replace) + c_lines[i + len(f_keys) :]
            return ReplaceResult(True, "\n".join(new_lines), "whitespace match")

    return ReplaceResult(False, content, "search text not found")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_code_edits.py -q`
Expected: PASS (9 passed). If a test fails, fix so behaviour matches the docstring's two-pass contract — do NOT weaken an assertion to make a test pass.

- [ ] **Step 6: Run `make check`** — Expected: green. Fix lint/type issues in the new files only.

- [ ] **Step 7: Commit**

```bash
git add src/autobot/tools/code/__init__.py src/autobot/tools/code/edits.py tests/unit/test_code_edits.py
git commit -m "feat(code): exact + whitespace-tolerant search/replace engine (#48)"
```

---

### Task 2: `read_file` (line-numbered) + `write_file` (create-only)

The two tools that don't need the engine. `read_file` returns `{n}\t{line}` numbered lines (so edits can cite line numbers) with optional paging; `write_file` **refuses to overwrite** an existing file (overwriting code is what `edit_file`/checkpoints are for — create-only removes the destructive-clobber path).

**Files:**
- Create: `src/autobot/tools/code/tools.py`
- Test: `tests/unit/test_code_tools.py`

**Interfaces:**
- Consumes: `AccessBroker.ensure(path, write) -> Path` (prompts internally on `NeedsAccessError`, or raises `AccessDeniedError`/`PermissionError` on refusal); `AccessDeniedError` from `autobot.tools.access`.
- Produces (Tasks 3 & 4 rely on these signatures):
  - `read_file(path: str, broker: AccessBroker, offset: int = 1, limit: int = 0) -> str`
  - `write_file(path: str, content: str, broker: AccessBroker) -> str`
  - module constants `_READ_CHAR_CAP = 100_000`, `_READ_LINE_CAP = 2000`.
  - helper `_read_text(resolved: Path) -> tuple[str | None, str]` — `(text, "")` or `(None, error)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_code_tools.py`:

```python
"""Tests for the code-editing tools (read/write/edit/multi_edit)."""

from __future__ import annotations

from pathlib import Path

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.tools import read_file, write_file


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_tools.py -q`
Expected: FAIL — `ImportError` (`read_file`/`write_file` not defined).

- [ ] **Step 3: Implement `tools.py` (read/write portion)**

Create `src/autobot/tools/code/tools.py`:

```python
"""Code-editing tools for the coder profile (path-jailed, OS-neutral).

Code-oriented siblings of the assistant's ``fileio`` tools, aligned with claude-code's
``FileReadTool``/``FileWriteTool``/``FileEditTool``: ``read_file`` returns
``{n}\\t{line}`` line-numbered text (so edits can cite lines), ``write_file`` is
**create-only** (never clobbers an existing file — that is what ``edit_file`` and, later,
checkpoints are for), and ``edit_file``/``multi_edit`` apply search/replace blocks via
:mod:`autobot.tools.code.edits`. Every path is resolved through the shared
:class:`~autobot.tools.access.AccessBroker`, so the workspace jail, folder grants, and
audit log apply exactly as they do for the assistant's file tools.
"""

from __future__ import annotations

from pathlib import Path

from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.code.edits import apply_replace

_log = get_logger("coder")

_READ_CHAR_CAP = 100_000  # max chars returned into the conversation
_READ_LINE_CAP = 2000  # max lines returned in one read_file call (claude-code default)


def _read_text(resolved: Path) -> tuple[str | None, str]:
    """Read a text file. Returns (text, "") on success or (None, error_message)."""
    if not resolved.exists():
        return None, f"There's no file at {resolved}."
    if resolved.is_dir():
        return None, f"'{resolved.name}' is a folder, not a file."
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        return None, f"I couldn't read {resolved.name}: {exc}"
    if b"\x00" in data[:4096]:
        return None, f"'{resolved.name}' looks like a binary file, so I can't read it as text."
    return data.decode("utf-8", errors="replace"), ""


def read_file(path: str, broker: AccessBroker, offset: int = 1, limit: int = 0) -> str:
    """Return a text file's contents, line-numbered (``{n}\\t{line}``), bounded.

    Args:
        path: File path (relative paths resolve against the active folder).
        broker: The access broker enforcing the workspace jail and grants.
        offset: 1-based first line to return (values < 1 are treated as 1).
        limit: Max lines to return; 0 (default) means up to ``_READ_LINE_CAP``.
    """
    if not path:
        return "Which file should I read? Tell me its path."
    try:
        resolved = broker.ensure(path, write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    text, err = _read_text(resolved)
    if text is None:
        return err
    lines = text.split("\n")
    if lines and lines[-1] == "":  # a final newline yields a trailing "" — not a line
        lines = lines[:-1]
    total = len(lines)
    start = max(1, offset)
    count = limit if limit and limit > 0 else _READ_LINE_CAP
    window = lines[start - 1 : start - 1 + count]
    numbered = "\n".join(f"{start + idx}\t{line}" for idx, line in enumerate(window))
    if len(numbered) > _READ_CHAR_CAP:
        numbered = numbered[:_READ_CHAR_CAP] + "\n…(truncated)"
    shown = start - 1 + len(window)
    tail = f"\n…({total - shown} more line(s); read with a higher offset)" if shown < total else ""
    _log.info("read_file name=%r lines=%d offset=%d", resolved.name, len(window), start)
    return f"{resolved.name} (lines {start}-{shown} of {total}):\n{numbered}{tail}"


def write_file(path: str, content: str, broker: AccessBroker) -> str:
    """Create a NEW text file (gated; create-only — refuses to overwrite an existing one)."""
    if not path:
        return "Where should I save it? Tell me the file path."
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if resolved.exists():
        return (
            f"'{resolved.name}' already exists — use edit_file or multi_edit to change it "
            "(write_file only creates new files)."
        )
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"I couldn't write {resolved.name}: {exc}"
    n = len(content)
    _log.info("write_file name=%r chars=%d", resolved.name, n)
    return f"Wrote {n} character{'s' if n != 1 else ''} to {resolved.name}."
```

- [ ] **Step 4: Run tests to verify they pass** — Run: `uv run pytest tests/unit/test_code_tools.py -q` — Expected: PASS (8).

- [ ] **Step 5: Run `make check`** — Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/code/tools.py tests/unit/test_code_tools.py
git commit -m "feat(code): line-numbered read_file + create-only write_file (#48)"
```

---

### Task 3: `edit_file` (with `replace_all`) + `multi_edit` (atomic, cascade-guarded)

Wire the engine to gated files. `edit_file` applies one search/replace (optionally `replace_all`); `multi_edit` applies a list of them to one file **atomically** — every edit applies to a working copy in order, the file is written only if all succeed, and a later edit's `find` may not be a substring of an earlier edit's `replace` (claude-code's cascade guard, which catches an edit that would match text a previous edit just inserted).

**Files:**
- Modify: `src/autobot/tools/code/tools.py`
- Test: `tests/unit/test_code_tools.py` (append)

**Interfaces:**
- Consumes: `apply_replace(content, find, replace, *, replace_all) -> ReplaceResult` (Task 1); `_read_text` (Task 2).
- Produces (Task 4 relies on these signatures):
  - `edit_file(path: str, find: str, replace: str, broker: AccessBroker, replace_all: bool = False) -> str`
  - `multi_edit(path: str, edits: list[dict[str, str]] | None, broker: AccessBroker) -> str`

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_code_tools.py`)**

Change the existing `from autobot.tools.code.tools import ...` line to include the new names, then append the tests:

```python
# extend the existing import:
from autobot.tools.code.tools import edit_file, multi_edit, read_file, write_file


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_tools.py -q`
Expected: FAIL — `ImportError` (`edit_file`/`multi_edit` not defined).

- [ ] **Step 3: Implement `edit_file` and `multi_edit` (append to `tools.py`)**

Append to `src/autobot/tools/code/tools.py`:

```python
def edit_file(
    path: str, find: str, replace: str, broker: AccessBroker, replace_all: bool = False
) -> str:
    """Replace ``find`` with ``replace`` in an EXISTING file (gated). See :mod:`.edits`."""
    if not path:
        return "Which file should I edit? Tell me its path."
    if not find:
        return "Tell me the exact text to replace (a non-empty `find`)."
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    text, err = _read_text(resolved)
    if text is None:
        return err
    result = apply_replace(text, find, replace, replace_all=replace_all)
    if not result.ok:
        return f"I couldn't edit {resolved.name}: {result.detail}."
    try:
        resolved.write_text(result.content, encoding="utf-8")
    except OSError as exc:
        return f"I couldn't save {resolved.name}: {exc}"
    _log.info("edit_file name=%r detail=%r", resolved.name, result.detail)
    return f"Edited {resolved.name} ({result.detail})."


def multi_edit(path: str, edits: list[dict[str, str]] | None, broker: AccessBroker) -> str:
    """Apply a list of ``{find, replace}`` edits to one file, atomically (all-or-nothing).

    Edits apply in order to a working copy; the file is written only if every edit matches.
    A failure (bad shape, no match, ambiguous match, or a ``find`` that is a substring of an
    earlier edit's ``replace``) writes nothing and reports which edit failed, so a partial
    edit can never corrupt the file.
    """
    if not path:
        return "Which file should I edit? Tell me its path."
    if not edits:
        return "No edits to apply — pass a list of {find, replace} objects."
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    text, err = _read_text(resolved)
    if text is None:
        return err
    working = text
    applied_replacements: list[str] = []
    for idx, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict) or "find" not in edit or "replace" not in edit:
            return f"Edit {idx} is malformed — each edit needs a `find` and a `replace`."
        find, replace = edit["find"], edit["replace"]
        if not isinstance(find, str) or not isinstance(replace, str) or not find:
            return f"Edit {idx} is malformed — `find` and `replace` must be text, `find` non-empty."
        probe = find.rstrip("\n")
        if probe and any(probe in prev for prev in applied_replacements):
            return (
                f"Edit {idx}'s search text was produced by an earlier edit; "
                "combine them into one edit or reorder them."
            )
        result = apply_replace(working, find, replace)
        if not result.ok:
            return f"Edit {idx} didn't apply ({result.detail}); nothing was changed."
        working = result.content
        applied_replacements.append(replace)
    try:
        resolved.write_text(working, encoding="utf-8")
    except OSError as exc:
        return f"I couldn't save {resolved.name}: {exc}"
    n = len(edits)
    _log.info("multi_edit name=%r edits=%d", resolved.name, n)
    return f"Applied {n} edit{'s' if n != 1 else ''} to {resolved.name}."
```

- [ ] **Step 4: Run tests to verify they pass** — Run: `uv run pytest tests/unit/test_code_tools.py -q` — Expected: PASS.

- [ ] **Step 5: Run `make check`** — Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/code/tools.py tests/unit/test_code_tools.py
git commit -m "feat(code): edit_file (replace_all) + atomic multi_edit with cascade guard (#48)"
```

---

### Task 4: `register_code_tools` (schemas, risk, no-arg-safe handlers)

Register the four tools with descriptions that teach the model when to use each (per-tool guidance, per `CLAUDE.md`), correct `Risk` levels, and `lambda` handlers with keyword defaults so a missing argument returns a message instead of raising. These are `coder`-profile tools, so `core=False` (advertised only when selected; the profile wiring is #53). This task does NOT wire them into `app.py`.

**Files:**
- Modify: `src/autobot/tools/code/tools.py`
- Modify: `src/autobot/tools/code/__init__.py` (re-export)
- Test: `tests/unit/test_code_tools.py` (append)

**Interfaces:**
- Consumes: `read_file`/`write_file`/`edit_file`/`multi_edit` (Tasks 2–3); `ToolRegistry`/`ToolSpec` (`autobot.tools.registry`); `Risk` (`autobot.core.types`).
- Produces: `register_code_tools(registry: ToolRegistry, broker: AccessBroker) -> None`.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_code_tools.py`)**

```python
# add to imports:
from autobot.core.types import Risk
from autobot.tools.code.tools import register_code_tools
from autobot.tools.registry import ToolRegistry


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
```

- [ ] **Step 2: Run tests to verify they fail** — Run: `uv run pytest tests/unit/test_code_tools.py -q` — Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement `register_code_tools` (append to `tools.py`)**

Add these imports with the existing imports at the top of `tools.py`:

```python
from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry, ToolSpec
```

Append the function:

```python
def register_code_tools(registry: ToolRegistry, broker: AccessBroker) -> None:
    """Register the coder-profile code tools (read/write/edit/multi_edit).

    All are gated (``core=False``) — advertised only when the tool selector judges them
    relevant — and route every path through ``broker`` for the workspace jail. The coder
    profile wires this in a later change (#53).
    """
    registry.register(
        ToolSpec(
            name="read_file",
            description=(
                "Read a source file's contents, line-numbered, so you can cite lines when "
                "editing. Cues: 'read/open/show X', 'what's in X'. Pass the file path; use "
                "`offset` (1-based first line) and `limit` (line count) to page through a large "
                "file. Read a file before editing it — edit_file matches against its current "
                "contents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to read."},
                    "offset": {"type": "integer", "description": "1-based first line (optional)."},
                    "limit": {"type": "integer", "description": "Max lines to return (optional)."},
                },
                "required": ["path"],
            },
            handler=lambda path="", offset=1, limit=0: read_file(path, broker, offset, limit),
            risk=Risk.READ_ONLY,
            ack="Reading that file.",
        )
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description=(
                "Create a NEW source file with the given content (it makes missing parent "
                "folders). This is create-only: it will NOT overwrite an existing file — to "
                "change an existing file use edit_file or multi_edit. Cues: 'create X', 'add a "
                "new file X'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the new file."},
                    "content": {"type": "string", "description": "The full text to write."},
                },
                "required": ["path", "content"],
            },
            handler=lambda path="", content="": write_file(path, content, broker),
            risk=Risk.WRITE,
            ack="Writing that file.",
        )
    )
    registry.register(
        ToolSpec(
            name="edit_file",
            description=(
                "Change an EXISTING file by replacing `find` with `replace`. `find` must "
                "uniquely identify one place — include enough surrounding lines to be "
                "unambiguous — unless you set `replace_all` to change every occurrence (e.g. "
                "renaming a symbol). Matching tolerates trailing-whitespace drift. Cues: "
                "'change/replace/fix A to B in X'. For several edits to one file, use multi_edit."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to edit."},
                    "find": {"type": "string", "description": "The text to find (non-empty)."},
                    "replace": {"type": "string", "description": "The replacement text."},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence (default false).",
                    },
                },
                "required": ["path", "find", "replace"],
            },
            handler=lambda path="", find="", replace="", replace_all=False: edit_file(
                path, find, replace, broker, replace_all
            ),
            risk=Risk.WRITE,
            ack="Editing that file.",
        )
    )
    registry.register(
        ToolSpec(
            name="multi_edit",
            description=(
                "Apply several find/replace edits to ONE file in a single atomic step — if any "
                "edit doesn't match, none are applied. Pass `edits` as a list of {find, replace} "
                "objects; they apply in order, each seeing the previous edit's result. Use this "
                "instead of repeated edit_file calls on the same file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to edit."},
                    "edits": {
                        "type": "array",
                        "description": "Edits applied in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string"},
                                "replace": {"type": "string"},
                            },
                            "required": ["find", "replace"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
            handler=lambda path="", edits=None: multi_edit(path, edits, broker),
            risk=Risk.WRITE,
            ack="Editing that file.",
        )
    )
    _log.info("code tools registered (read_file/write_file/edit_file/multi_edit)")
```

- [ ] **Step 4: Re-export from the package `__init__.py`**

Replace `src/autobot/tools/code/__init__.py` with:

```python
"""Code-editing tools for the coder profile (path-jailed, OS-neutral)."""

from __future__ import annotations

from autobot.tools.code.tools import register_code_tools

__all__ = ["register_code_tools"]
```

- [ ] **Step 5: Run tests to verify they pass** — Run: `uv run pytest tests/unit/test_code_tools.py -q` — Expected: PASS (all).

- [ ] **Step 6: Run `make check`** — Expected: green (whole suite).

- [ ] **Step 7: Commit**

```bash
git add src/autobot/tools/code/__init__.py src/autobot/tools/code/tools.py tests/unit/test_code_tools.py
git commit -m "feat(code): register coder-profile code tools with risk + schemas (#48)"
```

---

## Notes for the executor

- **Scope discipline (YAGNI):** #48 is *only* these four tools + the engine. Do **not** add `apply_patch`/unified-diff (optional in the spec — deferred), do **not** wire anything into `app.py` or a profile (that's #53), and do **not** add `grep`/`glob`/`run_command` (that's #49).
- **Reference-alignment, deliberately deferred (do NOT add in #48):** claude-code's `FileEditTool` also does **curly-quote normalization** (match straight↔curly quotes and preserve the file's style on write — `normalizeQuotes`/`findActualString`/`preserveQuoteStyle` in `/Users/mohamedjakkariyar/work/claude-code/src/tools/FileEditTool/utils.ts`). It's valuable for prose but lower-ROI for code and adds ~55 lines of open/close heuristics; we ship exact + trailing-whitespace now and will file a follow-up to port quote normalization if drift shows up. Also deferred: `replace_all` per-edit in `multi_edit`, and unified-diff `apply_patch`.
- **Why not Aider-style indentation reflow:** an earlier draft re-indented the replacement to a `strip()`-matched block. Dropped — claude-code doesn't reindent, it's a common source of edits landing in a subtly-wrong place, and read-before-edit gives the model the exact indentation. Exactness + clear errors is the proven, safer path.
- **Risk rationale (do not change without asking):** editing an existing file is `WRITE`, not `DESTRUCTIVE`, because the autonomy design auto-applies in-workspace edits and makes them recoverable via checkpoints (#51); the destructive-overwrite path is removed by making `write_file` create-only. Read is `READ_ONLY`.
- **Behaviour-preserving:** this package is purely additive and imported nowhere yet, so no existing test should change. If an existing test breaks, something was wired that shouldn't be — stop and report.
- The existing assistant `fileio.py` tools are intentionally left untouched; the code tools are separate so the two profiles can diverge.
