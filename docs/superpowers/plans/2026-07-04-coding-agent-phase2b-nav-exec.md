# Coding-agent Phase 2b — Navigation + execution tools (#49) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three more coder-profile tools to `src/autobot/tools/code/` — `glob` (list files by pattern), `grep` (search file contents), and `run_command` (run a shell command) — all path-jailed through the existing `AccessBroker`, cross-platform, output-bounded, and registered via the existing `register_code_tools`.

**Architecture:** Two new modules beside the Phase-2a edit tools. `search.py` holds `glob` + `grep` as **pure Python** (pathlib walk + `re`), so they need no external binary, work identically on macOS/Linux/Windows, and are exhaustively unit-testable; results are capped so a big tree can't flood the model. `shell.py` holds `run_command` behind an **injectable runner** (the repo's established `Callable` seam, so tests never spawn a real process); it picks the platform shell (`/bin/sh -c` on Unix, `cmd /c` on Windows), enforces a timeout and an output cap, runs in a jailed cwd, and is classified `Risk.DESTRUCTIVE` so the permission gate confirms it. Registration is folded into the existing `register_code_tools` via two small helper functions. Nothing is wired into `app.py`/a profile yet (that's #53).

**Tech Stack:** Python ≥ 3.11, stdlib only (`os`, `re`, `pathlib`, `sys`, `subprocess` — the last only inside the default runner). Existing `ToolRegistry`/`ToolSpec`/`Risk` and `AccessBroker`/`AccessDeniedError`. Tests: `pytest` with explicit fakes (no mocking framework; inject the runner).

## Global Constraints

Every task's requirements implicitly include this section (copied verbatim from `CLAUDE.md` and the epic's HARD RULES).

- **Conventional Commits**; **NO Co-Authored-By / no AI-attribution trailer** (repo convention).
- **No reference to any external tool/product** (a prior coding agent, its internal tool/file names, etc.) anywhere in committed code or docs. Describe what our code does in our own words.
- **Stage EXPLICIT paths only** — never `git add -A` / `.` / `-u`.
- **`make check` green** (ruff + ruff-format + mypy **strict** + pytest) before a task is DONE. `warn_unused_ignores=true` — a `# type: ignore` must be genuinely needed. Don't hand-format; run `make format`.
- `from __future__ import annotations` in **every** module; full type hints; **line length 100**; Google-style docstrings on public modules/functions (tests exempt).
- **Tools return strings and never raise out of the handler** — catch `AccessDeniedError`/`PermissionError`/`OSError`/`ValueError`/`re.error`/`subprocess`-timeouts and return a plain-English message. Registered `lambda` handlers must be **no-arg-safe** (keyword defaults).
- **Path jail is mandatory:** the root/cwd a tool operates in goes through `broker.ensure(path, write=...)`; reads/search use `write=False`, `run_command` uses `write=True` (it may modify the tree). `run_command` is genuinely powerful (a shell command can reach outside cwd), so it is `Risk.DESTRUCTIVE` — the permission gate is what contains it; the cwd jail only sets where it starts.
- **Cross-platform:** no macOS-only calls on this path; choose the shell by `sys.platform`; use `pathlib`/`os.walk` (not shelling out) for glob/grep.
- **Injectable subprocess:** `run_command` takes a `runner` seam (default shells out); unit tests inject a fake and NEVER spawn a real process.
- **Logging:** module-level `_log = get_logger("coder")`; seam events at INFO (tool name + counts/rc/timeout), not per-file/per-line noise. `%`-style args.
- **English only.** Tests: `uv run pytest <path> -q`.

---

## File Structure

- `src/autobot/tools/code/search.py` — `glob_files`, `grep` (pure Python) + `register_nav_tools(registry, broker)`.
- `src/autobot/tools/code/shell.py` — `run_command` (injectable runner) + `register_exec_tools(registry, broker)`.
- `src/autobot/tools/code/tools.py` — **modify** `register_code_tools` to also call `register_nav_tools` and `register_exec_tools`.
- `tests/unit/test_code_search.py` — glob + grep matrix.
- `tests/unit/test_code_shell.py` — run_command (fake runner) matrix.
- `tests/unit/test_code_tools.py` — **append** registration assertions for the three new tools.

Deliberately deferred (do NOT add in #49; the interfaces won't change when they land): **ripgrep acceleration** for `grep` (use `rg` when on PATH for speed on huge trees), **`.gitignore` awareness** in the pure walk, and **background-process management** for `run_command` (a long-running server / trailing `&`). Filed as follow-ups.

---

### Task 1: `glob` — list files by pattern (`search.py`)

Pure pathlib. Lists files matching a shell glob under a jailed root, newest first, capped.

**Files:**
- Create: `src/autobot/tools/code/search.py`
- Test: `tests/unit/test_code_search.py`

**Interfaces:**
- Produces (Tasks 2 & 4 rely on these): `glob_files(pattern: str, broker: AccessBroker, path: str = ".") -> str`; module constants `_GLOB_LIMIT = 100`, `_OUTPUT_CHAR_CAP = 60_000`; helper `_safe_mtime(p: Path) -> float`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_code_search.py`:

```python
"""Tests for the code navigation tools (glob + grep)."""

from __future__ import annotations

from pathlib import Path

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.search import glob_files


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


def _tree(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n")
    (root / "pkg" / "b.py").write_text("y = 2\n")
    (root / "readme.md").write_text("# hi\n")


def test_glob_lists_matching_files(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = glob_files("**/*.py", _broker(tmp_path), str(tmp_path))
    assert "a.py" in out and "b.py" in out
    assert "readme.md" not in out


def test_glob_no_matches(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = glob_files("**/*.rs", _broker(tmp_path), str(tmp_path))
    assert "no files" in out.lower()


def test_glob_denied_when_not_granted(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = glob_files("**/*.py", _broker(tmp_path, grant=False), str(tmp_path))
    assert "don't have access" in out.lower()


def test_glob_empty_pattern(tmp_path: Path) -> None:
    out = glob_files("", _broker(tmp_path), str(tmp_path))
    assert "pattern" in out.lower()


def test_glob_bad_pattern_does_not_raise(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = glob_files("/abs/pattern", _broker(tmp_path), str(tmp_path))  # non-relative → ValueError
    assert isinstance(out, str)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_search.py -q`
Expected: FAIL — `ImportError` (`glob_files` not defined).

- [ ] **Step 3: Implement `search.py` (glob portion)**

Create `src/autobot/tools/code/search.py`:

```python
"""File navigation tools for the coder profile: glob + grep (path-jailed, pure Python).

``glob`` lists files matching a shell-style pattern under a jailed root, newest first;
``grep`` searches file contents with a regular expression. Both walk the tree with
``pathlib``/``os`` (no external binary, so behaviour is identical on every OS) and cap
their results so a large tree can't flood the model. Every root is resolved through the
shared :class:`~autobot.tools.access.AccessBroker`.
"""

from __future__ import annotations

from pathlib import Path

from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError

_log = get_logger("coder")

_GLOB_LIMIT = 100  # max file paths returned by glob
_OUTPUT_CHAR_CAP = 60_000  # max chars returned by glob/grep into the conversation


def _safe_mtime(p: Path) -> float:
    """The file's mtime, or 0.0 if it can't be stat'd (never raises)."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def glob_files(pattern: str, broker: AccessBroker, path: str = ".") -> str:
    """List files matching a shell glob ``pattern`` under ``path`` (gated), newest first."""
    if not pattern:
        return "What should I match? Give a glob pattern like '**/*.py'."
    try:
        base = broker.ensure(path or ".", write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not base.is_dir():
        return f"'{base.name}' is not a folder to search."
    try:
        matches = [p for p in base.glob(pattern) if p.is_file()]
    except (OSError, ValueError) as exc:  # e.g. a non-relative or malformed pattern
        return f"I couldn't search with that pattern: {exc}"
    if not matches:
        return f"No files match {pattern!r} under {base}."
    matches.sort(key=_safe_mtime, reverse=True)
    shown = matches[:_GLOB_LIMIT]
    text = "\n".join(str(p) for p in shown)
    if len(text) > _OUTPUT_CHAR_CAP:
        text = text[:_OUTPUT_CHAR_CAP] + "\n…(truncated)"
    tail = f"\n…({len(matches) - len(shown)} more; narrow the pattern)" if len(matches) > len(shown) else ""
    _log.info("glob pattern=%r matches=%d", pattern, len(matches))
    return f"{len(matches)} file(s) matching {pattern!r} (newest first):\n{text}{tail}"
```

- [ ] **Step 4: Run tests to verify they pass** — Run: `uv run pytest tests/unit/test_code_search.py -q` — Expected: PASS (5).

- [ ] **Step 5: Run `make check`** — Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/code/search.py tests/unit/test_code_search.py
git commit -m "feat(code): glob tool — list files by pattern, jailed and capped (#49)"
```

---

### Task 2: `grep` — search file contents (`search.py`)

Pure Python regex search over the jailed tree, with three output modes, binary/large-file skipping, noise-dir pruning, and a result cap.

**Files:**
- Modify: `src/autobot/tools/code/search.py`
- Test: `tests/unit/test_code_search.py` (append)

**Interfaces:**
- Consumes: `_OUTPUT_CHAR_CAP` (Task 1).
- Produces (Task 4 relies on this): `grep(pattern: str, broker: AccessBroker, path: str = ".", glob: str | None = None, ignore_case: bool = False, output_mode: str = "files_with_matches") -> str`; constants `_GREP_LIMIT = 200`, `_GREP_MAX_FILE_BYTES = 1_000_000`, `_SKIP_DIRS`.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_code_search.py`)**

Update the import line and append tests:

```python
# extend the existing import:
from autobot.tools.code.search import glob_files, grep


def _grep_tree(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("import os\nfoo = 1\n")
    (root / "pkg" / "b.py").write_text("bar = 2\nfoo = 3\n")
    (root / "notes.txt").write_text("nothing here\n")


def test_grep_files_with_matches_default(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("foo", _broker(tmp_path), str(tmp_path))
    assert "a.py" in out and "b.py" in out
    assert "notes.txt" not in out


def test_grep_content_mode_has_file_line(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("foo", _broker(tmp_path), str(tmp_path), output_mode="content")
    assert "a.py:2:foo = 1" in out
    assert "b.py:2:foo = 3" in out


def test_grep_count_mode(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("foo", _broker(tmp_path), str(tmp_path), output_mode="count")
    assert ":1" in out  # each file has one match


def test_grep_glob_filter(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    (tmp_path / "pkg" / "c.md").write_text("foo in markdown\n")
    out = grep("foo", _broker(tmp_path), str(tmp_path), glob="*.py")
    assert "c.md" not in out
    assert "a.py" in out


def test_grep_ignore_case(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    (tmp_path / "pkg" / "d.py").write_text("FOO = 9\n")
    out = grep("foo", _broker(tmp_path), str(tmp_path), ignore_case=True, output_mode="content")
    assert "d.py:1:FOO = 9" in out


def test_grep_no_matches(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("zzz-not-here", _broker(tmp_path), str(tmp_path))
    assert "no matches" in out.lower()


def test_grep_bad_regex_does_not_raise(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("(unclosed", _broker(tmp_path), str(tmp_path))
    assert isinstance(out, str)
    assert "valid" in out.lower() or "pattern" in out.lower()


def test_grep_bad_output_mode(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("foo", _broker(tmp_path), str(tmp_path), output_mode="bogus")
    assert "output_mode" in out


def test_grep_skips_binary(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    (tmp_path / "pkg" / "blob.bin").write_bytes(b"foo\x00\x01binary")
    out = grep("foo", _broker(tmp_path), str(tmp_path))
    assert "blob.bin" not in out


def test_grep_denied_when_not_granted(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("foo", _broker(tmp_path, grant=False), str(tmp_path))
    assert "don't have access" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_search.py -q`
Expected: FAIL — `ImportError` (`grep` not defined).

- [ ] **Step 3: Implement `grep` (append to `search.py`)**

Add `import os` and `import re` to the top of `search.py` (with the existing imports), then append:

```python
_GREP_LIMIT = 200  # max result paths/lines/counts returned
_GREP_MAX_FILE_BYTES = 1_000_000  # skip files larger than this in the walk
_SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".ruff_cache", ".tox"}
)


def _iter_files(base: Path, glob_filter: str | None) -> list[Path]:
    """Files under ``base`` (noise dirs pruned, huge files skipped), optionally glob-filtered."""
    out: list[Path] = []
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            fp = Path(root) / name
            if glob_filter and not fp.match(glob_filter):
                continue
            try:
                if fp.stat().st_size > _GREP_MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out.append(fp)
    return out


def grep(
    pattern: str,
    broker: AccessBroker,
    path: str = ".",
    glob: str | None = None,
    ignore_case: bool = False,
    output_mode: str = "files_with_matches",
) -> str:
    """Search file contents under ``path`` for a regex ``pattern`` (gated, bounded).

    ``output_mode`` is ``"files_with_matches"`` (default — one path per matching file),
    ``"content"`` (``path:line:text`` per matching line), or ``"count"`` (``path:N``).
    ``glob`` filters which files are searched (e.g. ``"*.py"``); ``ignore_case`` is
    case-insensitive matching.
    """
    if not pattern:
        return "What should I search for? Give a regex or literal text."
    if output_mode not in ("files_with_matches", "content", "count"):
        return "output_mode must be 'files_with_matches', 'content', or 'count'."
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"That search pattern isn't valid: {exc}"
    try:
        base = broker.ensure(path or ".", write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not base.is_dir():
        return f"'{base.name}' is not a folder to search."

    results: list[str] = []
    truncated = False
    for fp in _iter_files(base, glob):
        try:
            data = fp.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:  # binary
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        hits = [(i + 1, ln) for i, ln in enumerate(lines) if rx.search(ln)]
        if not hits:
            continue
        if output_mode == "files_with_matches":
            results.append(str(fp))
        elif output_mode == "count":
            results.append(f"{fp}:{len(hits)}")
        else:  # content
            results.extend(f"{fp}:{lineno}:{ln}" for lineno, ln in hits)
        if len(results) >= _GREP_LIMIT:
            truncated = True
            del results[_GREP_LIMIT:]
            break

    if not results:
        return f"No matches for {pattern!r}."
    text = "\n".join(results)
    if len(text) > _OUTPUT_CHAR_CAP:
        text = text[:_OUTPUT_CHAR_CAP] + "\n…(truncated)"
        truncated = True
    tail = "\n…(results truncated; narrow the search or add a glob filter)" if truncated else ""
    _log.info("grep pattern=%r mode=%s results=%d", pattern, output_mode, len(results))
    return f"matches for {pattern!r} ({output_mode}):\n{text}{tail}"
```

- [ ] **Step 4: Run tests to verify they pass** — Run: `uv run pytest tests/unit/test_code_search.py -q` — Expected: PASS (glob + grep).

- [ ] **Step 5: Run `make check`** — Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/code/search.py tests/unit/test_code_search.py
git commit -m "feat(code): grep tool — regex search with 3 output modes, jailed and bounded (#49)"
```

---

### Task 3: `run_command` — cross-platform execution (`shell.py`)

Runs a shell command in a jailed cwd behind an injectable runner, with a clamped timeout and a bounded, combined stdout+stderr. Classified `Risk.DESTRUCTIVE` (set at registration in Task 4).

**Files:**
- Create: `src/autobot/tools/code/shell.py`
- Test: `tests/unit/test_code_shell.py`

**Interfaces:**
- Produces (Task 4 relies on these): `run_command(command: str, broker: AccessBroker, cwd: str = ".", timeout: float = _DEFAULT_TIMEOUT, runner: CommandRunner | None = None) -> str`; `CommandRunner = Callable[[str, str, float], tuple[int, str, bool]]` (command, cwd, timeout) → (returncode, combined_output, timed_out); constants `_DEFAULT_TIMEOUT = 120.0`, `_MAX_TIMEOUT = 600.0`, `_OUTPUT_CAP = 30_000`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_code_shell.py`:

```python
"""Tests for the code execution tool (run_command) — runner injected, no real process."""

from __future__ import annotations

from pathlib import Path

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.shell import CommandRunner, run_command


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


def _fake_runner(rc: int, out: str, timed_out: bool = False) -> CommandRunner:
    def run(command: str, cwd: str, timeout: float) -> tuple[int, str, bool]:
        return rc, out, timed_out

    return run


def test_run_command_success(tmp_path: Path) -> None:
    out = run_command("echo hi", _broker(tmp_path), str(tmp_path), runner=_fake_runner(0, "hi\n"))
    assert "hi" in out
    assert "ok" in out.lower()


def test_run_command_nonzero_exit_shows_status(tmp_path: Path) -> None:
    out = run_command(
        "false", _broker(tmp_path), str(tmp_path), runner=_fake_runner(1, "boom\n")
    )
    assert "exit 1" in out
    assert "boom" in out


def test_run_command_timeout(tmp_path: Path) -> None:
    out = run_command(
        "sleep 999", _broker(tmp_path), str(tmp_path), runner=_fake_runner(124, "partial", True)
    )
    assert "timed out" in out.lower()
    assert "partial" in out


def test_run_command_output_is_capped(tmp_path: Path) -> None:
    big = "x" * 50_000
    out = run_command("gen", _broker(tmp_path), str(tmp_path), runner=_fake_runner(0, big))
    assert "truncated" in out.lower()
    assert len(out) < 40_000


def test_run_command_empty(tmp_path: Path) -> None:
    out = run_command("   ", _broker(tmp_path), str(tmp_path), runner=_fake_runner(0, ""))
    assert "command" in out.lower()


def test_run_command_denied_when_not_granted(tmp_path: Path) -> None:
    out = run_command(
        "echo hi", _broker(tmp_path, grant=False), str(tmp_path), runner=_fake_runner(0, "hi\n")
    )
    assert "don't have access" in out.lower()


def test_run_command_timeout_is_clamped(tmp_path: Path) -> None:
    # A caller asking for 10_000s must be clamped to the max; the runner sees the clamp.
    seen: list[float] = []

    def run(command: str, cwd: str, timeout: float) -> tuple[int, str, bool]:
        seen.append(timeout)
        return 0, "ok", False

    run_command("echo hi", _broker(tmp_path), str(tmp_path), timeout=10_000.0, runner=run)
    assert seen == [600.0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_shell.py -q`
Expected: FAIL — `ImportError` (`run_command`/`CommandRunner` not defined).

- [ ] **Step 3: Implement `shell.py`**

Create `src/autobot/tools/code/shell.py`:

```python
"""Cross-platform command execution for the coder profile (gated, cwd-jailed).

``run_command`` runs one shell command in a jailed working directory and returns its
combined output, bounded. The command is genuinely powerful (a shell can reach outside
the cwd), so the tool is classified destructive and the permission gate is what contains
it — the cwd jail only sets where it starts. A ``runner`` seam is injected so command
assembly is unit-tested without spawning a real process; the default runner picks the
platform shell (``/bin/sh -c`` on Unix, ``cmd /c`` on Windows), applies the timeout, and
returns whether it timed out.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError

_log = get_logger("coder")

_DEFAULT_TIMEOUT = 120.0  # seconds
_MAX_TIMEOUT = 600.0
_OUTPUT_CAP = 30_000  # max chars of combined output returned

# (command, cwd, timeout) -> (returncode, combined_output, timed_out). Injectable for tests.
CommandRunner = Callable[[str, str, float], tuple[int, str, bool]]


def _default_runner(command: str, cwd: str, timeout: float) -> tuple[int, str, bool]:
    """Run ``command`` in the platform shell, capturing combined output (never raises)."""
    import subprocess

    argv = ["cmd", "/c", command] if sys.platform == "win32" else ["/bin/sh", "-c", command]
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        return 124, partial, True
    combined = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, combined, False


def run_command(
    command: str,
    broker: AccessBroker,
    cwd: str = ".",
    timeout: float = _DEFAULT_TIMEOUT,
    runner: CommandRunner | None = None,
) -> str:
    """Run a shell ``command`` in a jailed ``cwd`` (gated), returning bounded output."""
    if not command or not command.strip():
        return "What command should I run?"
    try:
        workdir = broker.ensure(cwd or ".", write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not workdir.is_dir():
        return f"'{workdir.name}' is not a folder to run in."
    limit = max(1.0, min(timeout or _DEFAULT_TIMEOUT, _MAX_TIMEOUT))
    run = runner or _default_runner
    try:
        rc, out, timed_out = run(command, str(workdir), limit)
    except OSError as exc:  # spawn failure (missing shell, etc.)
        return f"I couldn't run that command: {exc}"
    body = out if len(out) <= _OUTPUT_CAP else out[:_OUTPUT_CAP] + "\n…(output truncated)"
    _log.info("run_command rc=%d timed_out=%s chars=%d", rc, timed_out, len(out))
    if timed_out:
        return f"Command timed out after {int(limit)}s (partial output):\n{body}"
    status = "ok" if rc == 0 else f"exit {rc}"
    return f"[{status}]\n{body}" if body.strip() else f"[{status}] (no output)"
```

- [ ] **Step 4: Run tests to verify they pass** — Run: `uv run pytest tests/unit/test_code_shell.py -q` — Expected: PASS (7).

- [ ] **Step 5: Run `make check`** — Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/code/shell.py tests/unit/test_code_shell.py
git commit -m "feat(code): run_command — cross-platform shell exec, jailed + bounded (#49)"
```

---

### Task 4: Register `glob` / `grep` / `run_command`

Give each a `ToolSpec` (schema, `Risk`, `ack`, no-arg-safe handler) via helper functions in their modules, and call those from the existing `register_code_tools` so the coder profile still has one entry point.

**Files:**
- Modify: `src/autobot/tools/code/search.py` (add `register_nav_tools`)
- Modify: `src/autobot/tools/code/shell.py` (add `register_exec_tools`)
- Modify: `src/autobot/tools/code/tools.py` (call both from `register_code_tools`)
- Test: `tests/unit/test_code_tools.py` (append)

**Interfaces:**
- Consumes: `glob_files`/`grep` (search.py), `run_command` (shell.py), `ToolRegistry`/`ToolSpec`/`Risk`.
- Produces: `register_nav_tools(registry: ToolRegistry, broker: AccessBroker) -> None`; `register_exec_tools(registry: ToolRegistry, broker: AccessBroker) -> None`.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_code_tools.py`)**

The file already imports `Risk`, `ToolRegistry`, `register_code_tools`, and defines `_registry(tmp_path)`. Append:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_code_tools.py -q`
Expected: FAIL — `run_command`/`glob`/`grep` not registered yet.

- [ ] **Step 3: Add `register_nav_tools` to `search.py`**

Add `from autobot.core.types import Risk` and `from autobot.tools.registry import ToolRegistry, ToolSpec` to `search.py`'s imports, then append:

```python
def register_nav_tools(registry: ToolRegistry, broker: AccessBroker) -> None:
    """Register the navigation tools (glob, grep). Both are read-only and gated."""
    registry.register(
        ToolSpec(
            name="glob",
            description=(
                "List files whose path matches a shell glob (e.g. '**/*.py', 'src/**/*.ts'), "
                "newest first. Use this to find files by name/location before reading them. "
                "Pass `path` to search a subfolder. For searching file CONTENTS, use grep."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'."},
                    "path": {"type": "string", "description": "Folder to search in (optional)."},
                },
                "required": ["pattern"],
            },
            handler=lambda pattern="", path=".": glob_files(pattern, broker, path),
            risk=Risk.READ_ONLY,
            ack="Looking for files.",
        )
    )
    # A nested typed def (not a lambda) keeps the handler under the 100-char line limit
    # while staying no-arg-safe; it closes over ``broker``.
    def _grep_handler(
        pattern: str = "",
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = False,
        output_mode: str = "files_with_matches",
    ) -> str:
        return grep(pattern, broker, path, glob, ignore_case, output_mode)

    registry.register(
        ToolSpec(
            name="grep",
            description=(
                "Search file contents for a regular expression. `output_mode`: "
                "'files_with_matches' (default, paths only), 'content' (path:line:text), or "
                "'count' (path:N). Filter files with `glob` (e.g. '*.py'); set `ignore_case` "
                "for case-insensitive. Use this to find where code/text lives."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex or literal to search for."},
                    "path": {"type": "string", "description": "Folder to search in (optional)."},
                    "glob": {
                        "type": "string",
                        "description": "Only search files matching this glob.",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive (default false).",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content", "count"],
                        "description": "How to report matches (default files_with_matches).",
                    },
                },
                "required": ["pattern"],
            },
            handler=_grep_handler,
            risk=Risk.READ_ONLY,
            ack="Searching the code.",
        )
    )
    _log.info("nav tools registered (glob/grep)")
```

- [ ] **Step 4: Add `register_exec_tools` to `shell.py`**

Add `from autobot.core.types import Risk` and `from autobot.tools.registry import ToolRegistry, ToolSpec` to `shell.py`'s imports, then append:

```python
def register_exec_tools(registry: ToolRegistry, broker: AccessBroker) -> None:
    """Register the execution tool (run_command). Destructive → the gate confirms it."""
    registry.register(
        ToolSpec(
            name="run_command",
            description=(
                "Run a shell command (e.g. tests, a build, git, a linter) in the working "
                "folder and return its output. Cross-platform. Prefer the dedicated tools "
                "(read_file/edit_file/grep/glob) over shelling out for file work. Long-running "
                "or interactive commands aren't supported; keep it to commands that finish."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "cwd": {"type": "string", "description": "Folder to run in (optional)."},
                    "timeout": {
                        "type": "number",
                        "description": "Seconds before it's killed (default 120, max 600).",
                    },
                },
                "required": ["command"],
            },
            handler=lambda command="", cwd=".", timeout=_DEFAULT_TIMEOUT: run_command(
                command, broker, cwd, timeout
            ),
            risk=Risk.DESTRUCTIVE,
            ack="Running that command.",
        )
    )
    _log.info("exec tools registered (run_command)")
```

- [ ] **Step 5: Wire both into `register_code_tools` (`tools.py`)**

Add these imports to `tools.py` (with the existing imports):

```python
from autobot.tools.code.search import register_nav_tools
from autobot.tools.code.shell import register_exec_tools
```

At the END of `register_code_tools` (after the existing `_log.info("code tools registered ...")` line), add:

```python
    register_nav_tools(registry, broker)
    register_exec_tools(registry, broker)
```

- [ ] **Step 6: Run tests to verify they pass** — Run: `uv run pytest tests/unit/test_code_tools.py tests/unit/test_code_search.py tests/unit/test_code_shell.py -q` — Expected: PASS.

- [ ] **Step 7: Run `make check`** — Expected: green (whole suite).

- [ ] **Step 8: Commit**

```bash
git add src/autobot/tools/code/search.py src/autobot/tools/code/shell.py src/autobot/tools/code/tools.py tests/unit/test_code_tools.py
git commit -m "feat(code): register glob/grep/run_command for the coder profile (#49)"
```

---

## Notes for the executor

- **Scope discipline (YAGNI):** #49 is *only* these three tools + their registration. Do **not** wire anything into `app.py`/a profile (#53), and do **not** add the deferred items below.
- **Deliberately deferred (do NOT add in #49; interfaces won't change when they land):** ripgrep acceleration for `grep` (use `rg` when on PATH); `.gitignore` awareness in the walk; background/long-running process management for `run_command`; a `fixed_strings`/literal mode for `grep`.
- **Risk rationale (do not change without asking):** `glob`/`grep` are `READ_ONLY` (they only read). `run_command` is `DESTRUCTIVE` — a shell command can do anything, so the permission gate must confirm it every time; the command allow/blocklist that relaxes this for safe commands (e.g. `git`, `pytest`) is the security-gate work in #52.
- **Cross-platform:** glob/grep use `pathlib`/`os.walk` (identical everywhere); `run_command`'s default runner is the only OS-aware code (`sys.platform` picks the shell) and is never exercised in unit tests (they inject a fake runner).
- **Additive:** the only existing file touched is `tools.py` (two import lines + two calls at the end of `register_code_tools`). If anything else needs changing, stop and report.
