# Active workspace (working directory) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Jack a persistent **current working directory** (active folder) that relative file operations resolve against, settable by voice/chat or a native picker (within granted folders), shown in the chat UI — so file tools are meaningful instead of dumping into a fixed root.

**Architecture:** Approach A — add the cwd to the existing grant-based `AccessPolicy` (it already persists grants and is process-wide via `active_policy()`); make `AccessBroker.ensure` resolve relative paths against the cwd; migrate the `Sandbox`-jailed filesystem tools onto the broker (retiring `Sandbox`); add a `set_working_directory` tool, a system-prompt principle, a `WorkspaceEvent`, daemon `/workspace` endpoints, a chat-drawer folder chip + modal, and a Tauri `pick_folder` command.

**Tech Stack:** Python ≥3.11 (mypy strict, ruff), the `AccessPolicy`/`AccessBroker` (`tools/access.py`), the `EventBus` (`core/events.py`) over a WebSocket, FastAPI daemon (`daemon/server.py`), vanilla HTML/JS chat drawer (`ui/orb/chat.html`), Tauri (Rust) shell (`ui/orb-shell/src-tauri/src/main.rs`).

Design reference: `docs/plans/autobot_active_workspace_plan.md`. Closes #14.

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` at the top of every module.
- **mypy strict must stay green**; full type hints on new code.
- Google-style docstrings on public modules/classes/functions (ruff `D`; tests exempt). Line length 100. Run `make format`; never hand-format.
- **On-device only, English only.** No new off-device calls/dependencies. `set_working_directory` and the cwd path are local-only.
- Tools return strings and never raise out of `dispatch`; the broker turns `NeedsAccessError`/`AccessDeniedError`/`PermissionError` into friendly strings (existing pattern).
- Any genuinely-acting tool goes through the permission gate; `set_working_directory` is WRITE-class and reuses the existing grant-on-first-use card.
- `SYSTEM_PROMPT` holds only short, general principles; per-tool guidance lives in each `ToolSpec.description`.
- Commits: **Conventional Commits**, DCO sign-off (`git commit -s`), and **no `Co-Authored-By` trailer**.
- `make check` (ruff + ruff-format + mypy strict + pytest) must pass before each commit.
- Default cwd = the workspace (`~/.autobot/workspace`, `settings.sandbox_dir`); default behavior unchanged.

## File structure

| File | Responsibility | Change |
|---|---|---|
| `src/autobot/tools/access.py` | allowlist + **cwd** | Modify: `cwd`, `set_cwd`, `resolve`, cwd-aware `AccessBroker.ensure`, persist `{cwd,grants}`, `on_cwd_change` |
| `src/autobot/tools/filesystem.py` | filesystem tools | Modify: `FileTools(broker)` (relative→cwd via broker), updated descriptions, retire `Sandbox` use |
| `src/autobot/tools/sandbox.py` | (retired) | **Delete** once unreferenced |
| `src/autobot/tools/workspace.py` | the set-cwd tool | **Create**: `set_working_directory` + `register_workspace_tools` |
| `src/autobot/llm/ollama_llm.py` | shared prompt + cwd context | Modify: `SYSTEM_PROMPT` principle; `active_folder_line()`; inject in `_assemble` |
| `src/autobot/llm/anthropic_llm.py` | cwd context (cloud) | Modify: inject `active_folder_line()` in `_system` |
| `src/autobot/core/events.py` | engine→UI events | Modify: `WorkspaceEvent` + `publish_workspace` + `last_workspace` |
| `src/autobot/app.py` | composition root | Modify: reorder broker build; register filesystem+workspace tools on the broker; wire `on_workspace` |
| `src/autobot/daemon/server.py` | daemon HTTP/WS | Modify: `GET/POST /workspace`; send workspace frame on WS connect |
| `src/autobot/daemon/runner.py` | daemon wiring | Modify: `publish_workspace` callback → bus |
| `ui/orb/chat.html` | chat drawer | Modify: folder chip + modal + Reveal + Change-folder + `workspace` WS case |
| `ui/orb-shell/src-tauri/src/main.rs` | Tauri shell | Modify: `pick_folder` command (osascript) + register it |
| `tests/unit/test_access.py` | access tests | Modify: cwd/resolve/set_cwd/persistence tests |
| `tests/unit/test_filesystem.py` | filesystem tests | Modify: relative→cwd, migrated to broker |
| `tests/unit/test_workspace.py` | tool test | **Create** |
| `tests/unit/test_events.py` | event test | Modify: `publish_workspace` |
| `tests/unit/test_daemon_server.py` | daemon test | Modify: `/workspace` |

---

### Task 1: `AccessPolicy` gains a cwd (resolve + set_cwd + persistence)

**Files:**
- Modify: `src/autobot/tools/access.py`
- Test: `tests/unit/test_access.py`

**Interfaces:**
- Produces: `AccessPolicy(store_path, workspace_root, on_cwd_change: Callable[[Path], None] | None = None)`; properties `cwd: Path`; methods `set_cwd(path) -> Path`, `resolve(path) -> Path` (cwd-join + normalize, NO grant check). `AccessBroker.ensure(path, write)` now resolves relative paths against the cwd. `access.json` becomes `{"cwd": "<path>", "grants": [...]}`.

- [ ] **Step 1: Write the failing tests.** Add to `tests/unit/test_access.py`:

```python
def test_cwd_defaults_to_workspace(tmp_path) -> None:
    from autobot.tools.access import AccessPolicy

    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    assert pol.cwd == ws.resolve()


def test_resolve_joins_relative_onto_cwd(tmp_path) -> None:
    from autobot.tools.access import AccessPolicy

    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    assert pol.resolve("notes.txt") == (ws.resolve() / "notes.txt")
    # Absolute paths are returned resolved, not joined.
    abs_p = tmp_path / "elsewhere" / "a.txt"
    assert pol.resolve(str(abs_p)) == abs_p.resolve()


def test_set_cwd_requires_a_write_grant_then_persists(tmp_path) -> None:
    from autobot.tools.access import AccessPolicy, NeedsAccessError

    ws = tmp_path / "workspace"
    proj = tmp_path / "proj"
    proj.mkdir()
    store = tmp_path / "access.json"
    pol = AccessPolicy(store, ws)
    # Not granted yet -> refuses with NeedsAccessError (caller can prompt).
    try:
        pol.set_cwd(proj)
        raise AssertionError("expected NeedsAccessError")
    except NeedsAccessError:
        pass
    pol.grant(proj, write=True)
    assert pol.set_cwd(proj) == proj.resolve()
    # Persisted: a fresh policy over the same store loads the cwd back.
    assert AccessPolicy(store, ws).cwd == proj.resolve()


def test_load_falls_back_when_saved_cwd_is_invalid(tmp_path) -> None:
    from autobot.tools.access import AccessPolicy

    ws = tmp_path / "workspace"
    store = tmp_path / "access.json"
    store.write_text('{"cwd": "/nonexistent/gone", "grants": []}', encoding="utf-8")
    pol = AccessPolicy(store, ws)
    assert pol.cwd == ws.resolve()  # invalid saved cwd -> default workspace


def test_set_cwd_refuses_denylisted_path(tmp_path) -> None:
    from autobot.tools.access import AccessPolicy, AccessDeniedError

    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    try:
        pol.set_cwd(tmp_path / ".ssh")
        raise AssertionError("expected AccessDeniedError")
    except AccessDeniedError:
        pass


def test_broker_ensure_resolves_relative_against_cwd(tmp_path) -> None:
    from autobot.tools.access import AccessPolicy, AccessBroker

    class _Yes:
        def confirm(self, prompt, kind="danger"): return True
        def choose(self, prompt, options, kind="read", default="read"): return "write"

    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    broker = AccessBroker(pol, _Yes())
    # A relative path is created inside the cwd (the workspace, always granted).
    resolved = broker.ensure("sub/a.txt", write=True)
    assert resolved == (ws.resolve() / "sub" / "a.txt")
```

- [ ] **Step 2: Run the tests to verify they fail.**

Run: `uv run pytest tests/unit/test_access.py -k "cwd or resolve or fall_back or denylisted or relative" -v`
Expected: FAIL — `AccessPolicy` has no `cwd`/`set_cwd`/`resolve`; constructor rejects `on_cwd_change`; `ensure` doesn't join relative onto cwd.

- [ ] **Step 3: Add cwd to `AccessPolicy`.** In `src/autobot/tools/access.py`, change the constructor and add the methods. Replace the `__init__` and the `--- queries / mutations ---` section as follows.

Constructor (replace the existing `__init__`):

```python
    def __init__(
        self,
        store_path: str | Path,
        workspace_root: str | Path,
        on_cwd_change: Callable[[Path], None] | None = None,
    ) -> None:
        self._store = Path(store_path).expanduser()
        # The workspace is always available read-write and is the default cwd.
        self._workspace = Path(workspace_root).expanduser().resolve()
        self._workspace.mkdir(parents=True, exist_ok=True)  # was the Sandbox's job
        self._on_cwd_change = on_cwd_change
        self._lock = threading.RLock()
        self._grants: dict[Path, Mode] = {}
        self._cwd = self._workspace
        self._load()
```

Add `from collections.abc import Callable` to the imports at the top.

In `_load`, after loading grants, also restore the cwd with a validity fallback. Replace `_load` with:

```python
    def _load(self) -> None:
        try:
            data = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for g in data.get("grants", []):
            try:
                path = Path(str(g["path"])).expanduser().resolve()
                mode = Mode.WRITE if str(g.get("mode")) == "write" else Mode.READ
            except (KeyError, ValueError):
                continue
            self._grants[path] = mode
        saved = data.get("cwd")
        if isinstance(saved, str):
            cand = Path(saved).expanduser().resolve()
            # Only restore a cwd that still exists and is covered by a write grant
            # (the workspace always is); otherwise keep the default workspace.
            if cand.is_dir() and self._covered(cand, Mode.WRITE):
                self._cwd = cand
```

Replace `_save` to persist the cwd:

```python
    def _save(self) -> None:
        payload = {
            "cwd": str(self._cwd),
            "grants": [{"path": str(p), "mode": m.name.lower()} for p, m in self._grants.items()],
        }
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            self._store.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:  # never crash a turn over a save failure
            _log.warning("could not persist access state: %s", exc)
```

Add a `_covered` helper and the cwd API next to `_roots`/`_within`:

```python
    def _covered(self, resolved: Path, need: Mode) -> bool:
        """Whether ``resolved`` is inside a granted root with at least ``need`` mode."""
        best: Mode | None = None
        for root, mode in self._roots().items():
            if self._within(resolved, root):
                best = mode if best is None else max(best, mode)
        return best is not None and best >= need

    @property
    def cwd(self) -> Path:
        """The active working directory; relative paths resolve against it."""
        with self._lock:
            return self._cwd

    def resolve(self, path: str | Path) -> Path:
        """*Where*, not *whether*: relative paths join onto the cwd; then normalize.

        Expands ``~``, resolves symlinks, and collapses ``..``. Does NOT check grants
        (callers run :meth:`check` for that, so they can prompt on ``NeedsAccessError``).
        """
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.cwd / p
        return p.resolve()

    def set_cwd(self, path: str | Path) -> Path:
        """Set the active folder. Refuses a denylisted path; needs a write grant.

        Raises:
            AccessDeniedError: the path is on the secret denylist.
            NeedsAccessError: no write grant covers it (so the caller can prompt).
        """
        target = Path(path).expanduser().resolve()
        if _is_denied(target):
            raise AccessDeniedError(f"{target} is a protected location")
        if not self._covered(target, Mode.WRITE):
            raise NeedsAccessError(target, Mode.WRITE)
        with self._lock:
            self._cwd = target
            self._save()
        _log.info("active folder set to %s", target)
        if self._on_cwd_change is not None:
            self._on_cwd_change(target)
        return target
```

Refactor `check` to reuse `_covered` (replace its root-scan with a call), keeping behavior identical:

```python
    def check(self, path: str | Path, write: bool = False) -> Path:
        """Resolve ``path`` and confirm it's allowed for the requested op."""
        need = Mode.WRITE if write else Mode.READ
        resolved = Path(path).expanduser().resolve()
        if _is_denied(resolved):
            raise AccessDeniedError(f"{resolved} is a protected location")
        if self._covered(resolved, need):
            return resolved
        folder = resolved if resolved.is_dir() else resolved.parent
        raise NeedsAccessError(folder, need)
```

- [ ] **Step 4: Make `AccessBroker.ensure` cwd-aware.** In `AccessBroker.ensure`, resolve the path against the cwd before checking. Change the first lines of `ensure`:

```python
    def ensure(self, path: str | Path, write: bool = False) -> Path:
        """Return the resolved (cwd-relative) path if allowed, prompting if needed."""
        resolved = self._policy.resolve(path)  # join relative onto the active folder
        try:
            return self._policy.check(resolved, write)
        except NeedsAccessError as na:
```

(The rest of `ensure` is unchanged, except replace the two later `self._policy.check(path, write)` / `self._policy.grant(na.folder, ...)` calls' final `return self._policy.check(path, write)` with `return self._policy.check(resolved, write)`.)

- [ ] **Step 5: Run the tests to verify they pass.**

Run: `uv run pytest tests/unit/test_access.py -v`
Expected: PASS (new + existing access tests).

- [ ] **Step 6: `make check`, then commit.**

```bash
make check
git add src/autobot/tools/access.py tests/unit/test_access.py
git commit -s -m "feat(access): add a cwd (active folder) to AccessPolicy"
```

---

### Task 2: Migrate filesystem tools onto the broker (retire `Sandbox`)

**Files:**
- Modify: `src/autobot/tools/filesystem.py`, `src/autobot/app.py`
- Delete: `src/autobot/tools/sandbox.py`, `tests/unit/test_sandbox.py` (after confirming no other references)
- Test: `tests/unit/test_filesystem.py`

**Interfaces:**
- Consumes: `AccessBroker.ensure(path, write)` (Task 1, cwd-aware); `AccessPolicy.cwd`.
- Produces: `FileTools(broker: AccessBroker)`; `register_filesystem_tools(registry, broker) -> FileTools`.

- [ ] **Step 1: Write the failing test.** Replace the body of `tests/unit/test_filesystem.py` tests that construct `FileTools(Sandbox(...))` with broker-based ones. Add:

```python
def test_create_file_lands_in_active_folder(tmp_path) -> None:
    from autobot.tools.access import AccessPolicy, AccessBroker
    from autobot.tools.filesystem import FileTools

    class _Yes:
        def confirm(self, prompt, kind="danger"): return True
        def choose(self, prompt, options, kind="read", default="read"): return "write"

    ws = tmp_path / "workspace"
    proj = tmp_path / "proj"
    proj.mkdir()
    pol = AccessPolicy(tmp_path / "access.json", ws)
    pol.grant(proj, write=True)
    pol.set_cwd(proj)  # active folder is the project
    tools = FileTools(AccessBroker(pol, _Yes()))

    out = tools.create_file("demo.txt", "hi")
    assert (proj / "demo.txt").read_text() == "hi"  # landed in the active folder, not ws
    assert "demo.txt" in out


def test_create_file_defaults_to_workspace(tmp_path) -> None:
    from autobot.tools.access import AccessPolicy, AccessBroker
    from autobot.tools.filesystem import FileTools

    class _Yes:
        def confirm(self, prompt, kind="danger"): return True
        def choose(self, prompt, options, kind="read", default="read"): return "write"

    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)  # cwd defaults to ws
    tools = FileTools(AccessBroker(pol, _Yes()))
    tools.create_file("a.txt", "x")
    assert (ws / "a.txt").read_text() == "x"  # default behavior unchanged
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `uv run pytest tests/unit/test_filesystem.py::test_create_file_lands_in_active_folder -v`
Expected: FAIL — `FileTools` still takes a `Sandbox`, not a broker.

- [ ] **Step 3: Rewrite `filesystem.py` onto the broker.** Replace the `Sandbox`-based `FileTools` with a broker-based one. Full replacement of the class body + registration (the handlers now resolve relative→cwd via the broker; absolute paths are grant-checked):

```python
from __future__ import annotations

import shutil

from autobot.core.types import Risk
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.registry import ToolRegistry, ToolSpec

_PATH_PROP = {
    "type": "string",
    "description": "Path in the active folder (relative), or an absolute path elsewhere.",
}
_MAX_READ_BYTES = 20_000
_MAX_LIST_ENTRIES = 200


class FileTools:
    """Filesystem operations scoped by the access policy + active folder (cwd)."""

    def __init__(self, broker: AccessBroker) -> None:
        self._broker = broker

    def create_file(self, path: str, content: str = "") -> str:
        """Create (or overwrite) a file in the active folder (or a granted path)."""
        try:
            target = self._broker.ensure(path, write=True)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"created {target.name} ({len(content)} bytes) at {target}"

    def read_file(self, path: str) -> str:
        """Read a file's contents from the active folder (or a granted path)."""
        try:
            target = self._broker.ensure(path, write=False)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        if not target.exists():
            return f"not found: {path}"
        if target.is_dir():
            return f"that's a folder, not a file: {path}"
        data = target.read_text(encoding="utf-8", errors="replace")
        if len(data) > _MAX_READ_BYTES:
            data = data[:_MAX_READ_BYTES] + "\n…(truncated)"
        return f"{target.name} (at {target}):\n{data}"

    def list_files(self, subdir: str = "") -> str:
        """List files in the active folder (or a sub-folder / granted path)."""
        try:
            base = self._broker.ensure(subdir or ".", write=False)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        if not base.exists():
            return f"not found: {subdir or '.'}"
        if base.is_file():
            return f"{base.name} exists ({base.stat().st_size} bytes) at {base}"
        files = sorted(p for p in base.rglob("*") if p.is_file())
        if not files:
            return f"no files in {base}"
        shown = files[:_MAX_LIST_ENTRIES]
        lines = [f"{p.relative_to(base)} ({p.stat().st_size} bytes)" for p in shown]
        more = "" if len(files) <= _MAX_LIST_ENTRIES else f"\n…and {len(files) - len(shown)} more"
        return f"{len(files)} file(s) in {base}:\n" + "\n".join(lines) + more

    def move_file(self, source: str, destination: str) -> str:
        """Move or rename a file (within the active folder or granted paths)."""
        try:
            src = self._broker.ensure(source, write=True)
            dst = self._broker.ensure(destination, write=True)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        if not src.exists():
            return f"source not found: {source}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"moved {src.name} -> {dst.name} (now at {dst})"

    def delete_file(self, path: str) -> str:
        """Delete a file in the active folder (or a granted path); irreversible."""
        try:
            target = self._broker.ensure(path, write=True)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        if not target.exists():
            return f"not found: {path}"
        if target.is_dir():
            return f"refusing to delete a folder: {path}"
        target.unlink()
        gone = "confirmed gone" if not target.exists() else "but it still appears to exist"
        return f"deleted {target.name} ({gone})"

    def specs(self) -> list[ToolSpec]:
        """Tool specs with risk levels set; descriptions reflect the active folder."""
        return [
            ToolSpec(
                name="create_file",
                description=(
                    "Create a file in the user's ACTIVE folder (the current working "
                    "directory). Pass a relative name (e.g. 'notes.txt') to put it in the "
                    "active folder, or an absolute path to put it elsewhere (Jack asks to "
                    "grant a new folder on first use). Returns the file's full path."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": _PATH_PROP,
                        "content": {"type": "string", "description": "File contents."},
                    },
                    "required": ["path"],
                },
                handler=self.create_file,
                risk=Risk.WRITE,
                ack="Creating that file.",
            ),
            ToolSpec(
                name="read_file",
                description=(
                    "Read a file's contents from the active folder (relative name) or an "
                    "absolute path. Use it to check what a file contains or confirm it exists."
                ),
                parameters={
                    "type": "object",
                    "properties": {"path": _PATH_PROP},
                    "required": ["path"],
                },
                handler=self.read_file,
                risk=Risk.READ_ONLY,
            ),
            ToolSpec(
                name="list_files",
                description=(
                    "List files in the active folder (or a sub-folder / absolute path). Use "
                    "it to find a file or confirm one exists, e.g. after creating or deleting."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "subdir": {
                            "type": "string",
                            "description": "Optional sub-folder; omit for the active folder.",
                        }
                    },
                },
                handler=self.list_files,
                risk=Risk.READ_ONLY,
            ),
            ToolSpec(
                name="move_file",
                description="Move or rename a file (active folder, or granted absolute paths).",
                parameters={
                    "type": "object",
                    "properties": {"source": _PATH_PROP, "destination": _PATH_PROP},
                    "required": ["source", "destination"],
                },
                handler=self.move_file,
                risk=Risk.WRITE,
            ),
            ToolSpec(
                name="delete_file",
                description="Delete a file in the active folder (or a granted path). Cannot be undone.",
                parameters={
                    "type": "object",
                    "properties": {"path": _PATH_PROP},
                    "required": ["path"],
                },
                handler=self.delete_file,
                risk=Risk.DESTRUCTIVE,
            ),
        ]


def register_filesystem_tools(registry: ToolRegistry, broker: AccessBroker) -> FileTools:
    """Register the filesystem tools (scoped by the access policy + active folder)."""
    tools = FileTools(broker)
    for spec in tools.specs():
        registry.register(spec)
    return tools
```

- [ ] **Step 4: Reorder `app.py` so the broker is built before registering filesystem tools.** In `src/autobot/app.py`:

  (a) Remove the early lines 365-366 (`sandbox = Sandbox(...)` and `register_filesystem_tools(registry, sandbox)`); remove `from autobot.tools.sandbox import Sandbox` (line 33).
  (b) Change the `AccessPolicy` construction (was line 370) to compute the workspace root directly and wire `on_workspace` (added as a `build` param in Task 4 — for now pass `on_cwd_change=None`; Task 4 wires it):

```python
    from pathlib import Path as _Path

    workspace_root = _Path(settings.sandbox_dir).expanduser().resolve()
    access_policy = AccessPolicy(settings.access_store, workspace_root, on_cwd_change=None)
    set_active_policy(access_policy)
```

  (c) After the `gate = PermissionGate(...)` block (currently ends line 505), build the broker **unconditionally** and register the filesystem tools on it; keep the file-io block reusing the same broker:

```python
    from autobot.tools.access import AccessBroker

    broker = AccessBroker(access_policy, confirmer)
    register_filesystem_tools(registry, broker)  # now active-folder aware

    if settings.allow_file_io:
        from autobot.tools.fileio import register_file_io_tools

        register_file_io_tools(registry, broker)
        log.info("file I/O ENABLED (read/copy/write/edit, access-gated)")
```

  Update the import at top: `from autobot.tools.filesystem import register_filesystem_tools` stays; remove the in-conditional `from autobot.tools.access import AccessBroker` (now at the top of the broker block).

- [ ] **Step 5: Delete the retired `Sandbox` after confirming no references.**

Run: `grep -rnE "Sandbox|tools\.sandbox|from autobot.tools.sandbox" src/ tests/`
Expected: only `tests/unit/test_sandbox.py` (and possibly stale doc strings). If only those, delete both files:

```bash
git rm src/autobot/tools/sandbox.py tests/unit/test_sandbox.py
```

If `grep` shows any remaining `src/` import, fix it to use the broker/policy before deleting.

- [ ] **Step 6: Run the tests, then `make check`.**

Run: `uv run pytest tests/unit/test_filesystem.py tests/unit/test_access.py -v` (PASS), then `make check`.

- [ ] **Step 7: Commit.**

```bash
git add -A
git commit -s -m "feat(files): resolve filesystem tools against the active folder; retire Sandbox"
```

---

### Task 3: `set_working_directory` tool + prompt principle + cwd in context

**Files:**
- Create: `src/autobot/tools/workspace.py`
- Modify: `src/autobot/llm/ollama_llm.py` (SYSTEM_PROMPT + `active_folder_line` + `_assemble`), `src/autobot/llm/anthropic_llm.py` (`_system`), `src/autobot/app.py` (register the tool)
- Test: `tests/unit/test_workspace.py` (create), `tests/unit/test_llm_parsing.py`

**Interfaces:**
- Consumes: `AccessBroker.ensure`, `AccessPolicy.set_cwd`/`cwd`, `active_policy()`.
- Produces: `set_working_directory(path, broker, policy) -> str`; `register_workspace_tools(registry, broker, policy)`; `active_folder_line() -> str` in `ollama_llm`.

- [ ] **Step 1: Write the failing test.** Create `tests/unit/test_workspace.py`:

```python
"""Tests for the set_working_directory tool."""

from __future__ import annotations

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.workspace import set_working_directory


class _Yes:
    def confirm(self, prompt, kind="danger"): return True
    def choose(self, prompt, options, kind="read", default="read"): return "write"


class _No:
    def confirm(self, prompt, kind="danger"): return False
    def choose(self, prompt, options, kind="read", default="read"): return ""


def test_set_working_directory_grants_and_sets(tmp_path) -> None:
    ws = tmp_path / "workspace"
    proj = tmp_path / "proj"
    proj.mkdir()
    pol = AccessPolicy(tmp_path / "access.json", ws)
    out = set_working_directory(str(proj), AccessBroker(pol, _Yes()), pol)
    assert pol.cwd == proj.resolve()
    assert proj.name in out


def test_set_working_directory_declined_leaves_cwd(tmp_path) -> None:
    ws = tmp_path / "workspace"
    proj = tmp_path / "proj"
    proj.mkdir()
    pol = AccessPolicy(tmp_path / "access.json", ws)
    out = set_working_directory(str(proj), AccessBroker(pol, _No()), pol)
    assert pol.cwd == ws.resolve()  # unchanged
    assert "access" in out.lower() or "couldn't" in out.lower()
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `uv run pytest tests/unit/test_workspace.py -v`
Expected: FAIL — `autobot.tools.workspace` doesn't exist.

- [ ] **Step 3: Create the tool.** Write `src/autobot/tools/workspace.py`:

```python
"""The set-working-directory tool: move Jack's active folder (grant-gated)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError, AccessPolicy
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("tools")


def set_working_directory(path: str, broker: AccessBroker, policy: AccessPolicy) -> str:
    """Set Jack's active folder; grants write access to a new folder on first use."""
    if not path or not path.strip():
        return "Tell me which folder to work in."
    try:
        folder = broker.ensure(path, write=True)  # prompts + grants on first use
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not folder.is_dir():
        return f"That's not a folder: {folder}"
    try:
        policy.set_cwd(folder)
    except (AccessDeniedError, Exception) as exc:  # noqa: BLE001 - never raise out
        _log.warning("set_working_directory failed: %s", exc)
        return f"I couldn't switch to that folder: {exc}"
    _log.info("active folder set via tool name=%r", folder.name)
    return f"Working in {folder.name} now ({folder})."


def register_workspace_tools(
    registry: ToolRegistry, broker: AccessBroker, policy: AccessPolicy
) -> None:
    """Register the set_working_directory tool."""
    registry.register(
        ToolSpec(
            name="set_working_directory",
            description=(
                "Set the ACTIVE folder Jack works in — where create_file/list_files/etc. "
                "operate by default. Use when the user says 'work in <folder>', 'switch to my "
                "<name> project', 'use this folder', 'set my workspace to <path>'. Pass the "
                "folder path; Jack asks to grant a new folder on first use."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the folder to work in."}
                },
                "required": ["path"],
            },
            handler=lambda path: set_working_directory(path, broker, policy),
            risk=Risk.WRITE,
            ack="Switching folder.",
        )
    )
    _log.info("workspace tool registered (set_working_directory)")
```

- [ ] **Step 4: Register it in `app.py`.** Right after `register_filesystem_tools(registry, broker)` (Task 2 Step 4c), add:

```python
    from autobot.tools.workspace import register_workspace_tools

    register_workspace_tools(registry, broker, access_policy)
```

- [ ] **Step 5: Run the tool test.**

Run: `uv run pytest tests/unit/test_workspace.py -v`
Expected: PASS.

- [ ] **Step 6: Add the prompt principle + cwd-in-context.** In `src/autobot/llm/ollama_llm.py`, add a bullet to `SYSTEM_PROMPT` right after the multi-step principle (the bullet that starts "- You can take several steps…"):

```python
        "- You work in an ACTIVE folder (your current working directory). Create and "
        "edit files there by default. If the user asks to save something clearly "
        "unrelated to that folder, ask whether to save it there or pick another place.\n"
```

Add a helper near `system_prompt`:

```python
def active_folder_line() -> str:
    """A one-line 'Active folder: <path>' for the system context, or '' if unknown."""
    from autobot.tools.access import active_policy

    pol = active_policy()
    return f"Active folder: {pol.cwd}" if pol is not None else ""
```

In `_assemble` (after the memory-profile block, before the summary block), inject it:

```python
        folder = active_folder_line()
        if folder:
            messages.append({"role": "system", "content": folder})
```

In `src/autobot/llm/anthropic_llm.py` `_system`, add the same after the memory block:

```python
        from autobot.llm.ollama_llm import active_folder_line

        folder = active_folder_line()
        if folder:
            parts.append(folder)
```

- [ ] **Step 7: Test the prompt + commit.** Add to `tests/unit/test_llm_parsing.py`:

```python
def test_system_prompt_mentions_active_folder() -> None:
    from autobot.llm.ollama_llm import system_prompt

    assert "active folder" in system_prompt("chat").lower()
```

Run: `uv run pytest tests/unit/test_workspace.py tests/unit/test_llm_parsing.py -v` (PASS), then `make check`.

```bash
git add -A
git commit -s -m "feat(workspace): set_working_directory tool + active-folder prompt and context"
```

---

### Task 4: `WorkspaceEvent` + bus + `on_workspace` wiring

**Files:**
- Modify: `src/autobot/core/events.py`, `src/autobot/app.py`, `src/autobot/daemon/runner.py`
- Test: `tests/unit/test_events.py`

**Interfaces:**
- Produces: `WorkspaceEvent(path: str, name: str)` with `.message()`; `EventBus.publish_workspace(path: str, name: str)`; `EventBus.last_workspace -> dict | None`. Wire shape `{"type":"workspace","path":<full>,"name":<basename>}`. `build(..., on_workspace: Callable[[str, str], None] | None = None)`.

- [ ] **Step 1: Write the failing test.** Add to `tests/unit/test_events.py`:

```python
def test_publish_workspace_emits_and_remembers_last() -> None:
    from autobot.core.events import EventBus

    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    bus.publish_workspace("/Users/me/proj", "proj")
    assert seen == [{"type": "workspace", "path": "/Users/me/proj", "name": "proj"}]
    assert bus.last_workspace == {"type": "workspace", "path": "/Users/me/proj", "name": "proj"}
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `uv run pytest tests/unit/test_events.py::test_publish_workspace_emits_and_remembers_last -v`
Expected: FAIL — no `publish_workspace`.

- [ ] **Step 3: Add the event + publisher.** In `src/autobot/core/events.py`, add after `StepEvent`:

```python
@dataclass(frozen=True, slots=True)
class WorkspaceEvent:
    """The active folder (cwd) — for the chat drawer's folder chip."""

    path: str
    name: str

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {"type": "workspace", "path": self.path, "name": self.name}
```

In `EventBus.__init__`, add `self._last_workspace: dict[str, object] | None = None`. Add a property + publisher (next to `last_state` / `publish_step`):

```python
    @property
    def last_workspace(self) -> dict[str, object] | None:
        """The most recently published workspace frame, or None."""
        with self._lock:
            return self._last_workspace

    def publish_workspace(self, path: str, name: str) -> None:
        """Record and broadcast the active folder (drives the chat folder chip)."""
        msg = WorkspaceEvent(path, name).message()
        with self._lock:
            self._last_workspace = msg
        self._emit(msg)
```

- [ ] **Step 4: Wire `on_workspace` through `app.py`.** Add the param to `build(...)` (after `on_step`):

```python
    on_workspace: Callable[[str, str], None] | None = None,
```

Change the `AccessPolicy` construction (Task 2 Step 4b) to wire the callback:

```python
    def _cwd_changed(p: _Path) -> None:
        if on_workspace is not None:
            on_workspace(str(p), p.name)

    access_policy = AccessPolicy(settings.access_store, workspace_root, on_cwd_change=_cwd_changed)
```

- [ ] **Step 5: Wire the daemon callback.** In `src/autobot/daemon/runner.py`, add next to `publish_step`:

```python
    def publish_workspace(path: str, name: str) -> None:
        bus.publish_workspace(path, name)
```

and pass it to `build(...)` (after `on_step=publish_step,`): `on_workspace=publish_workspace,`.

- [ ] **Step 6: Run tests + `make check` + commit.**

Run: `uv run pytest tests/unit/test_events.py -v` (PASS), then `make check`.

```bash
git add -A
git commit -s -m "feat(events): add a workspace (active-folder) event to the bus"
```

---

### Task 5: Daemon `/workspace` endpoints + connect frame

**Files:**
- Modify: `src/autobot/daemon/server.py`
- Test: `tests/unit/test_daemon_server.py`

**Interfaces:**
- Consumes: `active_policy()` (cwd + grants), `on_action(tool, args)` (runs `set_working_directory` through the gate), `bus.last_workspace`.
- Produces: `GET /workspace` → `{"path", "name", "grants": [...]}`; `POST /workspace {path}` → `{"ok", "result"}`.

- [ ] **Step 1: Write the failing test.** Add to `tests/unit/test_daemon_server.py` (follow the existing test client/app-construction pattern in that file — use the same `build_app(...)`/`TestClient` helper the access-endpoint tests use):

```python
def test_get_workspace_reports_cwd() -> None:
    # Use the file's existing helper to build the FastAPI app + TestClient with an
    # active AccessPolicy set (mirror test_get_access in this file).
    client = _client_with_active_policy()  # existing helper / inline per this file's pattern
    resp = client.get("/workspace")
    assert resp.status_code == 200
    body = resp.json()
    assert "path" in body and "name" in body and "grants" in body
```

(Model this test on the existing `/access` GET test in `test_daemon_server.py`; if that file constructs the app via a local helper, reuse it. If `/access` has no test there, add a minimal one for `/workspace` using the same `create_app(...)` entrypoint the file already imports.)

- [ ] **Step 2: Run it to verify it fails.**

Run: `uv run pytest tests/unit/test_daemon_server.py -k workspace -v`
Expected: FAIL — no `/workspace` route.

- [ ] **Step 3: Add the endpoints.** In `src/autobot/daemon/server.py`, add two handlers next to `get_access`/`post_access_grant`:

```python
    async def get_workspace() -> dict[str, Any]:
        """Report the active folder (cwd) + granted folders (for the chat folder modal)."""
        from autobot.tools.access import active_policy

        pol = active_policy()
        if pol is None:
            return {"path": "", "name": "", "grants": []}
        grants = [{"path": g.path, "mode": g.mode.name.lower()} for g in pol.grants()]
        return {"path": str(pol.cwd), "name": pol.cwd.name, "grants": grants}

    async def post_workspace(request: Request) -> dict[str, Any]:
        """Set the active folder (``{path}``) through the gate (grant card applies)."""
        payload = await request.json()
        if not isinstance(payload, dict) or "path" not in payload or on_action is None:
            return {"ok": False, "error": "expected {path} / action unavailable"}
        result = await asyncio.to_thread(on_action, "set_working_directory", {"path": str(payload["path"])})
        return {"ok": True, "result": result}
```

Register the routes (next to the `/access` route registrations):

```python
    app.add_api_route("/workspace", get_workspace, methods=["GET"])
    app.add_api_route("/workspace", post_workspace, methods=["POST"])
```

- [ ] **Step 4: Send the workspace frame on WS connect.** In the WebSocket handler (where it sends the current state on connect — near `bus.last_state`), also send the last workspace if present:

```python
        if bus.last_workspace is not None:
            await websocket.send_json(bus.last_workspace)
```

(Place it right after the existing initial state-send so a freshly-opened chat drawer shows the current folder.)

- [ ] **Step 5: Run tests + `make check` + commit.**

Run: `uv run pytest tests/unit/test_daemon_server.py -v` (PASS), then `make check`.

```bash
git add -A
git commit -s -m "feat(daemon): /workspace endpoints + send active folder on connect"
```

---

### Task 6: Chat drawer — folder chip + modal + Reveal + Change folder

**Files:**
- Modify: `ui/orb/chat.html`

**Interfaces:**
- Consumes: the `{"type":"workspace", path, name}` WS frame (Task 4/5); `GET/POST /workspace` (Task 5); the existing `$()` helper, the header structure, the `.ctx-detail` modal pattern, the `ws.onmessage` `m.type` switch, the Tauri `reveal_in_finder` command, and `pick_folder` (Task 7).
- Produces: a folder chip + modal. **No automated test** — manual verification (Step 5).

> Note: `grep` does not work on `ui/orb/chat.html` (binary-ish). Use the Read tool and `awk '/pat/{print NR": "$0}'` to locate anchors. The Tauri bridge is `window.__TAURI__` / `tauri()` (see existing `reveal`/`open_external` usage in the file).

- [ ] **Step 1: Add CSS.** In the `<style>` block (near the `.ctx`/`.ctx-detail` rules, ~line 35-44), add:

```css
  .folder{display:inline-flex;align-items:center;gap:5px;margin-left:10px;-webkit-app-region:no-drag;
    cursor:pointer;color:var(--muted);font-size:12px;}
  .folder svg{width:14px;height:14px;display:block;}
  .folder .fname{max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .folder-detail{position:absolute;top:46px;left:12px;z-index:6;background:var(--panel);
    border:.5px solid var(--field-line);border-radius:10px;padding:10px 12px;min-width:240px;
    box-shadow:0 6px 20px rgba(0,0,0,.35);}
  .folder-detail .path{font-size:12px;color:var(--text);word-break:break-all;margin-bottom:8px;}
  .folder-detail .grants{font-size:11px;color:var(--muted);margin-bottom:8px;}
  .folder-detail .acts{display:flex;gap:8px;}
  .folder-detail button{border:.5px solid var(--field-line);background:var(--field);color:var(--text);
    font-size:12px;border-radius:7px;padding:5px 9px;cursor:pointer;}
```

- [ ] **Step 2: Add the chip + modal markup.** In the header (next to the context chip `.ctx`, in the `<header>`), add a folder chip element with a folder icon and a `.fname` span (id `folderName`), plus a hidden `.folder-detail` modal container (id `folderDetail`) holding a `.path` (id `folderPath`), a `.grants` (id `folderGrants`), and two buttons: "Reveal in Finder" (id `folderReveal`) and "Change folder…" (id `folderChange`). Mirror the existing `.ctx` / `.ctx-detail` markup that's already in the header.

- [ ] **Step 3: Add the JS.** In the `<script>`, near `renderContext`/`showChoices`, add:

```javascript
  // Active-folder chip + modal. Driven by the "workspace" WS frame and GET /workspace.
  var workspacePath = "";
  function renderWorkspace(m){
    workspacePath = m.path || "";
    var chip = $("folder"); if(!chip) return;
    chip.style.display = workspacePath ? "inline-flex" : "none";
    var nm = $("folderName"); if(nm) nm.textContent = m.name || "";
  }
  async function openFolderDetail(){
    var d = $("folderDetail"); if(!d) return;
    try {
      var r = await fetch(API + "/workspace"); var w = await r.json();
      workspacePath = w.path || workspacePath;
      $("folderPath").textContent = w.path || "(none)";
      var grants = (w.grants || []).map(function(g){ return g.path + " (" + g.mode + ")"; });
      $("folderGrants").textContent = grants.length ? ("Granted: " + grants.join(", ")) : "";
    } catch(e){}
    d.classList.toggle("hidden");
  }
  function revealWorkspace(){ if(workspacePath && tauri()) tauri().invoke("reveal_in_finder", {path: workspacePath}); }
  async function changeFolder(){
    if(!tauri()) return;
    var picked; try { picked = await tauri().invoke("pick_folder"); } catch(e){ return; }
    if(!picked) return;  // cancelled
    try { await fetch(API + "/workspace", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({path: picked})}); } catch(e){}
    // The engine emits a "workspace" frame on success; the chip updates from it.
  }
```

Wire the click handlers (near the other `addEventListener` calls):

```javascript
  if($("folder")) $("folder").addEventListener("click", openFolderDetail);
  if($("folderReveal")) $("folderReveal").addEventListener("click", revealWorkspace);
  if($("folderChange")) $("folderChange").addEventListener("click", changeFolder);
```

- [ ] **Step 4: Route the WS frame + fetch initial state.** In the `ws.onmessage` `m.type` switch (the `connect()` function, ~line 562-566), add:

```javascript
        else if(m.type === "workspace") renderWorkspace(m);
```

After `connect();`, fetch the current workspace once so the chip shows on load:

```javascript
  (async function(){ try { var r = await fetch(API + "/workspace"); var w = await r.json(); if(w.path) renderWorkspace(w); } catch(e){} })();
```

- [ ] **Step 5: Verify edits are well-formed; `make check` (Python untouched); commit.**

Re-read the edited regions: balanced braces, the `workspace` case routes to `renderWorkspace`, the modal toggles, handlers are wired. Run `make check` (must stay green — no Python changed). Live GUI verification (chip shows the folder, modal lists grants, Reveal opens Finder, Change-folder picker switches the cwd) is **deferred to the user** (needs the running app + Task 7).

```bash
git add ui/orb/chat.html
git commit -s -m "feat(ui): active-folder chip + modal (reveal, change folder) in the chat drawer"
```

---

### Task 7: Tauri `pick_folder` command (native folder picker)

**Files:**
- Modify: `ui/orb-shell/src-tauri/src/main.rs`

**Interfaces:**
- Produces: a `#[tauri::command] fn pick_folder() -> Option<String>` returning the chosen folder's POSIX path (or `None` on cancel), invoked from `chat.html` as `tauri().invoke("pick_folder")`.

- [ ] **Step 1: Add the command.** In `ui/orb-shell/src-tauri/src/main.rs`, near `reveal_in_finder` (~line 210), add:

```rust
#[tauri::command]
fn pick_folder() -> Option<String> {
    // Native folder chooser via AppleScript (same shell-out style as reveal_in_finder;
    // no extra Tauri plugin). Returns the POSIX path, or None if the user cancels.
    let out = std::process::Command::new("osascript")
        .arg("-e")
        .arg("POSIX path of (choose folder with prompt \"Choose a folder for Jack to work in\")")
        .output()
        .ok()?;
    if !out.status.success() {
        return None; // user cancelled (osascript exits non-zero)
    }
    let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if path.is_empty() { None } else { Some(path) }
}
```

- [ ] **Step 2: Register it.** In the `tauri::generate_handler![...]` list (~line 285), add `pick_folder` alongside `reveal_in_finder`, `open_external`, etc.

- [ ] **Step 3: Verify it builds.**

Run: `cd ui/orb-shell/src-tauri && cargo check` (from the repo root: `cargo check --manifest-path ui/orb-shell/src-tauri/Cargo.toml`)
Expected: compiles with no errors.

- [ ] **Step 4: Commit.**

```bash
git add ui/orb-shell/src-tauri/src/main.rs
git commit -s -m "feat(ui): native folder picker (pick_folder) in the Tauri shell"
```

- [ ] **Step 5: Manual end-to-end (deferred to the user).** Launch the app, open the chat folder chip → Change folder… → pick a folder → confirm the grant card (first time) → the chip + file ops now target that folder; Reveal in Finder opens it.

---

## Self-review

**Spec coverage** (against `docs/plans/autobot_active_workspace_plan.md`):
- §3.1 cwd on AccessPolicy + resolve/set_cwd + persistence + retire Sandbox → **Tasks 1, 2**. ✓
- §3.2 set_working_directory tool + prompt principle + cwd-in-context → **Task 3**. ✓
- §3.3 WorkspaceEvent + bus → **Task 4**; daemon `/workspace` + connect frame → **Task 5**; chat chip/modal/Reveal/Change → **Task 6**; Tauri picker → **Task 7**. ✓
- §4 decisions: persist cwd (Task 1), default workspace (Tasks 1-2), default-to-cwd-ask-when-unsure (Task 3 prompt), voice/chat + picker (Tasks 3, 6, 7), osascript picker (Task 7). ✓
- §5 testing: access (Task 1), filesystem relative→cwd (Task 2), tool (Task 3), events (Task 4), daemon (Task 5), UI manual (Tasks 6-7). ✓
- §6 risk — retiring Sandbox: Task 2 Step 5 greps for references before deleting; AccessPolicy.resolve+check is the boundary (Task 1 tests). ✓

**Placeholder scan:** engine tasks (1-4) carry complete code + exact tests. Task 5's test references the file's existing app-build helper (named, not invented) — the implementer reuses the `/access`-test pattern in that file. Tasks 6-7 (UI/Rust) provide complete additive code with named anchors and an explicit manual-verification step (the `chat.html` markup in Task 6 Step 2 is described against the existing `.ctx`/`.ctx-detail` structure rather than pasted, since that file is not greppable — the implementer reads it and mirrors it).

**Type consistency:** `AccessPolicy(store, workspace_root, on_cwd_change)`, `resolve(path)->Path`, `set_cwd(path)->Path`, `cwd:Path` are used consistently across Tasks 1-5; `WorkspaceEvent(path,name)` / `publish_workspace(path,name)` / `on_workspace(path,name)` / `GET /workspace`→`{path,name,grants}` align across Tasks 4-6; `pick_folder()->Option<String>` (Task 7) matches the `tauri().invoke("pick_folder")` call (Task 6).

**Note on a known overlap:** with cwd-relative `create_file` (Task 2) and the existing `write_file` (`fileio.py`, also broker-based), the two tools now overlap (both create files, both relative→cwd). This is acceptable for this feature; their descriptions steer the model (create_file = active folder by name; write_file = explicit content to a path). A future issue may consolidate them (and trim the ~40-tool surface).
