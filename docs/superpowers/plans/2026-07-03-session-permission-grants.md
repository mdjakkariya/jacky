# Session Permission Grants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the permission gate remember a destructive action for the session ("Allow once / Allow this session / Cancel") so a batch cleanup asks once per action+folder, and stop mutating tools from reporting silent failures as success.

**Architecture:** The `PermissionGate` gains an in-memory, per-`(tool, folder)` grant set checked before it prompts. A new tri-state `confirm_action() -> "once"|"session"|""` on the confirmer expresses the grant decision (with a `getattr` fallback to the existing bool `confirm()`, so legacy confirmers and all current tests are untouched). Network/egress actions are excluded — they always confirm per call. Separately, `delete_file`/`move_file` raise a new `ToolError` for not-found/denied so the registry marks the result `ok=False` instead of a masked `ok=True`.

**Tech Stack:** Python ≥3.11 (mypy strict, ruff), pytest; the orb UI is vanilla JS tested with vitest.

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` in every module.
- Full type hints; **mypy strict must stay green**.
- Google-style docstrings on public modules/classes/functions (ruff `D` rules; tests exempt).
- Line length 100; formatting/imports owned by ruff — run `make format`, never hand-format.
- Value objects are `frozen=True, slots=True` dataclasses.
- Tools return strings and never raise *out of* `dispatch`; errors become failed `ToolResult`s.
- **On-device only.** Network/egress confirmations must NOT gain a session grant (privacy).
- **English only.**
- Commit messages: Conventional Commits (`feat:`, `fix:`, `docs:`, …). **No `Co-Authored-By` trailer.**
- `make check` (ruff + ruff-format + mypy strict + pytest) must pass before a task is done.
- UI tests run with `cd ui && npx vitest run <path>`.

---

### Task 1: `ToolError` — let a handler signal an expected failure

**Files:**
- Modify: `src/autobot/tools/registry.py` (add `ToolError`; extend `dispatch` at `128-157`)
- Create: `tests/unit/test_registry.py`

**Interfaces:**
- Produces: `ToolError(Exception)` in `autobot.tools.registry`; `ToolRegistry.dispatch` returns `ToolResult(ok=False, content=<message>)` when a handler raises `ToolError` (no `"tool failed:"` prefix), and keeps the existing `ok=False, content="tool failed: {exc}"` for any other exception.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_registry.py`:

```python
"""Tests for ToolRegistry dispatch, incl. ToolError -> failed result mapping."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.tools.registry import ToolError, ToolRegistry, ToolSpec


def _registry_with(handler: object, name: str = "t") -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolSpec(name=name, description="", parameters={}, handler=handler, risk=Risk.WRITE))
    return reg


def test_tool_error_becomes_failed_result_without_prefix() -> None:
    def boom(**_kw: object) -> str:
        raise ToolError("no file named x; nothing was removed")

    result = _registry_with(boom).dispatch("t", {})
    assert result.ok is False
    assert result.content == "no file named x; nothing was removed"
    assert "tool failed" not in result.content


def test_unexpected_exception_still_prefixed() -> None:
    def boom(**_kw: object) -> str:
        raise ValueError("kaboom")

    result = _registry_with(boom).dispatch("t", {})
    assert result.ok is False
    assert result.content.startswith("tool failed:")


def test_successful_handler_is_ok() -> None:
    result = _registry_with(lambda **_kw: "done").dispatch("t", {})
    assert result.ok is True and result.content == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mohamedjakkariyar/work/browserstack/autobot && python -m pytest tests/unit/test_registry.py -v`
Expected: FAIL with `ImportError: cannot import name 'ToolError'`.

- [ ] **Step 3: Add `ToolError` and handle it in `dispatch`**

In `src/autobot/tools/registry.py`, add after the imports (near line 20):

```python
class ToolError(Exception):
    """Raised by a handler to report an *expected* failure (e.g. not found, denied).

    The registry maps it to a failed :class:`ToolResult` whose ``content`` is the
    message verbatim — so the model (and the audit log) see ``ok=False`` instead of a
    success-looking string, without the generic ``"tool failed:"`` prefix used for
    unexpected crashes.
    """
```

Then change the `try` block in `dispatch` (currently lines ~152-157) to:

```python
        try:
            content = spec.handler(**(arguments or {}))
            return ToolResult(name=name, content=content, ok=True)
        except ToolError as exc:  # expected failure — report verbatim, ok=False
            return ToolResult(name=name, content=str(exc), ok=False)
        except Exception as exc:  # unexpected — surface, don't crash the loop
            return ToolResult(name=name, content=f"tool failed: {exc}", ok=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_registry.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/registry.py tests/unit/test_registry.py
git commit -m "feat(tools): add ToolError so handlers can report expected failures"
```

---

### Task 2: Truthful `delete_file` / `move_file` results (issue #40 defect 2)

**Files:**
- Modify: `src/autobot/tools/filesystem.py` (`move_file` at `84-95`, `delete_file` at `97-109`)
- Modify: `tests/unit/test_filesystem.py` (update 4 tests that assert the old masked-success strings)

**Interfaces:**
- Consumes: `ToolError` from Task 1.
- Produces: `delete_file` and `move_file` raise `ToolError` on not-found or access-denied (so `dispatch` yields `ok=False`). Success strings ("deleted …", "moved …") and the folder-refusal message are unchanged.

- [ ] **Step 1: Update the failing tests to the new (correct) contract**

The file already has `_tools(tmp_path) -> FileTools` (approves via `_Yes()`) and
`_denied_broker(tmp_path) -> (broker, pol)` (via `_No()`). Add near the top of
`tests/unit/test_filesystem.py`:

```python
import pytest

from autobot.tools.registry import ToolError
```

Replace `test_delete_missing_file_is_reported_not_raised` (lines ~103-104) with:

```python
def test_delete_missing_file_raises_tool_error(tmp_path: Path) -> None:
    # A missing target must be a FAILURE, not a success-looking string (issue #40):
    # otherwise dispatch records ok=True and the model over-claims "deleted".
    with pytest.raises(ToolError):
        _tools(tmp_path).delete_file("nope.txt")
```

Replace `test_delete_file_denied_returns_message` (lines ~241-247) with:

```python
def test_delete_file_denied_raises_tool_error(tmp_path: Path) -> None:
    broker, _pol = _denied_broker(tmp_path)
    tools = FileTools(broker)
    with pytest.raises(ToolError) as ei:
        tools.delete_file("notes.txt")
    assert "access" in str(ei.value).lower() or "don't" in str(ei.value).lower()
```

Replace `test_move_file_missing_source_returns_message` (lines ~227-231) with:

```python
def test_move_file_missing_source_raises_tool_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        _tools(tmp_path).move_file("ghost.txt", "dest.txt")
```

Replace `test_move_file_denied_returns_message` (lines ~233-239) with:

```python
def test_move_file_denied_raises_tool_error(tmp_path: Path) -> None:
    broker, _pol = _denied_broker(tmp_path)
    tools = FileTools(broker)
    with pytest.raises(ToolError) as ei:
        tools.move_file("a.txt", "b.txt")
    assert "access" in str(ei.value).lower() or "don't" in str(ei.value).lower()
```

Add one dispatch-level test proving the masked-success bug is gone end-to-end:

```python
def test_delete_missing_reports_failure_through_dispatch(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    reg = ToolRegistry()
    for spec in tools.specs():
        reg.register(spec)
    result = reg.dispatch("delete_file", {"path": "nope.txt"})
    assert result.ok is False  # was ok=True before the fix
    assert "deleted" not in result.content.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_filesystem.py -v`
Expected: FAIL — the raise-expecting tests fail because the handlers still return strings.

- [ ] **Step 3: Make the handlers raise `ToolError`**

In `src/autobot/tools/filesystem.py`, add the import (top, with the other `autobot.tools` imports):

```python
from autobot.tools.registry import ToolError, ToolRegistry, ToolSpec
```

Rewrite `move_file` (lines 84-95):

```python
    def move_file(self, source: str, destination: str) -> str:
        """Move or rename a file (within the active folder or granted paths)."""
        try:
            src = self._broker.ensure(source, write=True)
            dst = self._broker.ensure(destination, write=True)
        except (AccessDeniedError, PermissionError) as exc:
            raise ToolError(str(exc)) from exc
        if not src.exists():
            raise ToolError(f"couldn't move — no file named {source}; nothing was moved")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"moved {src.name} -> {dst.name} (now at {dst})"
```

Rewrite `delete_file` (lines 97-109):

```python
    def delete_file(self, path: str) -> str:
        """Delete a file in the active folder (or a granted path); irreversible."""
        try:
            target = self._broker.ensure(path, write=True)
        except (AccessDeniedError, PermissionError) as exc:
            raise ToolError(str(exc)) from exc
        if not target.exists():
            raise ToolError(
                f"couldn't delete — no file named {path} at {target.parent}; nothing was removed"
            )
        if target.is_dir():
            return f"refusing to delete a folder: {path}"
        target.unlink()
        gone = "confirmed gone" if not target.exists() else "but it still appears to exist"
        return f"deleted {target.name} ({gone})"
```

> The docstring on `filesystem.py` says handlers "never raise out of the method"; that
> guarantee is now provided by `dispatch` catching `ToolError`. Update the module
> docstring line about caught errors to mention `ToolError` for accuracy.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_filesystem.py tests/unit/test_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/filesystem.py tests/unit/test_filesystem.py
git commit -m "fix(files): report not-found/denied delete & move as failures (#40)"
```

---

### Task 3: `confirm_action` on the confirmer protocol + built-in confirmers

**Files:**
- Modify: `src/autobot/tools/permission.py` (`Confirmer` protocol `30-47`; `TerminalConfirmer` `50-62`; `AlwaysAllow` `65-74`; `AlwaysDeny` `77-86`)
- Modify: `tests/unit/test_permission_gate.py` (add confirm_action coverage for the stubs)

**Interfaces:**
- Produces: `Confirmer.confirm_action(self, prompt: str, kind: str = "danger") -> str` returning `"once"`, `"session"`, or `""`. `TerminalConfirmer` → `"once"` on yes else `""`; `AlwaysAllow` → `"once"`; `AlwaysDeny` → `""`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_permission_gate.py`:

```python
def test_builtin_confirmers_confirm_action() -> None:
    from autobot.tools.permission import AlwaysAllow, AlwaysDeny

    assert AlwaysAllow().confirm_action("go?") == "once"
    assert AlwaysDeny().confirm_action("go?") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_permission_gate.py::test_builtin_confirmers_confirm_action -v`
Expected: FAIL with `AttributeError: 'AlwaysAllow' object has no attribute 'confirm_action'`.

- [ ] **Step 3: Add `confirm_action` to the protocol and stubs**

In `src/autobot/tools/permission.py`, add to the `Confirmer` protocol (after `choose`, ~line 47):

```python
    def confirm_action(self, prompt: str, kind: str = "danger") -> str:
        """Confirm a gated action: "once" (proceed), "session" (proceed + remember), "" (cancel)."""
        ...
```

Add to `TerminalConfirmer`:

```python
    def confirm_action(self, prompt: str, kind: str = "danger") -> str:
        """Terminal has no session button: a yes proceeds once, anything else cancels."""
        return "once" if self.confirm(prompt) else ""
```

Add to `AlwaysAllow`:

```python
    def confirm_action(self, prompt: str, kind: str = "danger") -> str:  # noqa: D102
        return "once"
```

Add to `AlwaysDeny`:

```python
    def confirm_action(self, prompt: str, kind: str = "danger") -> str:  # noqa: D102
        return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_permission_gate.py -v`
Expected: PASS (existing tests unaffected — they use `confirm()`-only stubs which the gate reaches via fallback in Task 4).

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/permission.py tests/unit/test_permission_gate.py
git commit -m "feat(gate): add tri-state confirm_action to the Confirmer protocol"
```

---

### Task 4: Session grants in `PermissionGate`

**Files:**
- Modify: `src/autobot/tools/permission.py` (`PermissionGate.__init__` `92-109`; `execute` confirm block `170-202`)
- Modify: `tests/unit/test_permission_gate.py`

**Interfaces:**
- Consumes: `confirm_action` (Task 3).
- Produces:
  - `PermissionGate(__init__)` gains `scope_of: Callable[[ToolCall], str] | None = None`.
  - `PermissionGate.clear_session_grants() -> None`.
  - Behavior: for a **non-network** call needing confirmation, the gate builds key `f"{call.name}|{scope}"` (scope from `scope_of(call)` or `""`); if the key is already granted it dispatches without prompting (audited); otherwise it calls `confirm_action`; `"session"` records the key, `"once"` proceeds without recording, `""` declines. **Network calls keep `confirm()` and never record a grant.**

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_permission_gate.py`:

```python
class _ScriptedConfirmer:
    """Returns queued confirm_action answers; records how many times it was asked."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = answers
        self.asks = 0

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        self.asks += 1
        return bool(self._answers)

    def confirm_action(self, prompt: str, kind: str = "danger") -> str:
        self.asks += 1
        return self._answers.pop(0) if self._answers else ""


def _delete_gate(confirmer: object, scope_of: object | None = None):
    tool = _SpyTool(Risk.DESTRUCTIVE)
    registry = ToolRegistry()
    registry.register(ToolSpec(name="delete_file", description="", parameters={}, handler=tool, risk=Risk.DESTRUCTIVE))
    gate = PermissionGate(
        registry, AuditLog(":memory:"), confirmer,  # type: ignore[arg-type]
        scope_of=scope_of,  # type: ignore[arg-type]
    )
    return gate, tool


def test_session_grant_skips_second_confirmation() -> None:
    scope_of = lambda call: str(call.arguments.get("path", ""))  # noqa: E731
    confirmer = _ScriptedConfirmer(["session"])
    gate, tool = _delete_gate(confirmer, scope_of)
    r1 = gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    r2 = gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    assert r1.ok and r2.ok
    assert confirmer.asks == 1  # asked once; second was auto-approved


def test_once_does_not_remember() -> None:
    scope_of = lambda call: str(call.arguments.get("path", ""))  # noqa: E731
    confirmer = _ScriptedConfirmer(["once", "once"])
    gate, _ = _delete_gate(confirmer, scope_of)
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    assert confirmer.asks == 2  # asked every time


def test_session_grant_is_scoped_by_key() -> None:
    scope_of = lambda call: str(call.arguments.get("path", ""))  # noqa: E731
    confirmer = _ScriptedConfirmer(["session", "session"])
    gate, _ = _delete_gate(confirmer, scope_of)
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/other/b"}))
    assert confirmer.asks == 2  # different scope -> asked again


def test_clear_session_grants_forgets() -> None:
    scope_of = lambda call: str(call.arguments.get("path", ""))  # noqa: E731
    confirmer = _ScriptedConfirmer(["session", "session"])
    gate, _ = _delete_gate(confirmer, scope_of)
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    gate.clear_session_grants()
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    assert confirmer.asks == 2


def test_legacy_confirm_only_confirmer_still_works() -> None:
    # A confirmer with no confirm_action falls back to confirm(); never grants a session.
    gate, tool = _delete_gate(_RecordingConfirmer())  # confirm() -> False
    result = gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    assert not tool.ran and not result.ok


def test_network_write_never_offers_session() -> None:
    tool = _SpyTool(Risk.WRITE)
    registry = ToolRegistry()
    registry.register(ToolSpec(name="send", description="", parameters={}, handler=tool, risk=Risk.WRITE, network=True))
    confirmer = _ScriptedConfirmer(["session", "session"])  # would grant if asked via confirm_action
    gate = PermissionGate(registry, AuditLog(":memory:"), confirmer, scope_of=lambda c: "x")  # type: ignore[arg-type]
    gate.execute(ToolCall(name="send", arguments={}))
    gate.execute(ToolCall(name="send", arguments={}))
    assert confirmer.asks == 2  # network path uses confirm() each time, no grant
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_permission_gate.py -k "session or legacy or network_write" -v`
Expected: FAIL (`scope_of` is not a valid kwarg / `clear_session_grants` missing).

- [ ] **Step 3: Implement session grants in the gate**

In `src/autobot/tools/permission.py`, extend `PermissionGate.__init__` signature and body:

```python
    def __init__(
        self,
        registry: ToolRegistry,
        audit: AuditLog,
        confirmer: Confirmer,
        confirm_at_or_above: Risk = Risk.DESTRUCTIVE,
        permission_status: Callable[[str], str] | None = None,
        on_permission_needed: Callable[[str], object] | None = None,
        scope_of: Callable[[ToolCall], str] | None = None,
    ) -> None:
        self._registry = registry
        self._audit = audit
        self._confirmer = confirmer
        self._threshold = confirm_at_or_above
        self._permission_status = permission_status
        self._on_permission_needed = on_permission_needed
        # Derives a per-call scope string (e.g. the target folder) for session grants;
        # None -> tool-name-only scope. Set in the composition root (app.build).
        self._scope_of = scope_of
        # Actions the user approved "for this session" (in-memory; key = "tool|scope").
        # Cleared on New Chat and on process restart.
        self._session_grants: set[str] = set()
```

Add two helpers (after `ack_of`, before `execute`):

```python
    def clear_session_grants(self) -> None:
        """Forget every "allow this session" grant (called on New Chat / restart)."""
        self._session_grants.clear()

    def _grant_key(self, call: ToolCall) -> str:
        """The session-grant key for a call: ``"{tool}|{scope}"`` (scope may be empty)."""
        scope = self._scope_of(call) if self._scope_of is not None else ""
        return f"{call.name}|{scope}"

    def _confirm_action(self, prompt: str, kind: str) -> str:
        """Ask the confirmer for a tri-state decision, falling back to bool confirm()."""
        fn = getattr(self._confirmer, "confirm_action", None)
        if callable(fn):
            return str(fn(prompt, kind))
        return "once" if self._confirmer.confirm(prompt, kind) else ""
```

Replace the confirm block in `execute` (currently lines 170-202) with:

```python
        if spec.risk >= self._threshold or (spec.network and spec.risk >= Risk.WRITE):
            prompt = spec.confirm_prompt or self._format_prompt(
                spec.name, spec.risk, call.arguments
            )
            kind = self._confirm_kind(spec)
            granted = False
            if spec.network:
                # Off-device send: always confirm per call, never remembered (privacy).
                decision = "once" if self._confirmer.confirm(prompt, kind) else ""
            else:
                key = self._grant_key(call)
                if key in self._session_grants:
                    granted = True
                    decision = "once"  # already approved this session — skip the card
                else:
                    decision = self._confirm_action(prompt, kind)
                    if decision == "session":
                        self._session_grants.add(key)
            if not decision:
                timed_out = bool(getattr(self._confirmer, "timed_out", False))
                reason = "timeout" if timed_out else "user_declined"
                _log.info("denied tool=%s risk=%s reason=%s", call.name, spec.risk.name, reason)
                self._audit.log(
                    tool=call.name,
                    arguments=call.arguments,
                    risk=spec.risk.name,
                    decision=Decision.DENIED,
                    ok=None,
                    detail="timed out without confirmation" if timed_out else "declined by user",
                )
                if timed_out:
                    content = (
                        "No confirmation was received in time, so the action was "
                        "cancelled and NOT performed. Tell the user, in one short "
                        "sentence, that you cancelled it because you didn't get a "
                        "confirmation. Do not ask again or retry."
                    )
                else:
                    content = (
                        "The user declined this action, so it was not performed. "
                        "Acknowledge in one short sentence; do not ask again or retry."
                    )
                return ToolResult(name=call.name, content=content, ok=False)
            if granted:
                _log.info("auto-approved tool=%s via session grant", call.name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_permission_gate.py -v`
Expected: PASS (new session tests + all pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/permission.py tests/unit/test_permission_gate.py
git commit -m "feat(gate): remember 'allow this session' grants per action+folder (#40)"
```

---

### Task 5: `folder_scope_of` + wire it into the gate

**Files:**
- Modify: `src/autobot/tools/access.py` (add module-level `folder_scope_of`)
- Modify: `src/autobot/app.py` (`PermissionGate(...)` at `588-594`)
- Modify: `tests/unit/test_access.py`

**Interfaces:**
- Consumes: `AccessPolicy.resolve`; `autobot.core.types.ToolCall`.
- Produces: `folder_scope_of(policy: AccessPolicy) -> Callable[[ToolCall], str]` — for a call with a `path` string argument, returns `str(policy.resolve(path).parent)`; otherwise `""`. Wired as `scope_of` on the gate in `app.build`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_access.py` (reuse the file's existing `AccessPolicy` construction pattern; a `tmp_path`-based policy):

```python
def test_folder_scope_of_uses_target_parent(tmp_path: Path) -> None:
    from autobot.core.types import ToolCall
    from autobot.tools.access import AccessPolicy, folder_scope_of

    policy = AccessPolicy(tmp_path / "access.json", tmp_path / "ws")
    scope = folder_scope_of(policy)
    key = scope(ToolCall(name="delete_file", arguments={"path": str(tmp_path / "Desktop" / "a.png")}))
    assert key == str((tmp_path / "Desktop").resolve())


def test_folder_scope_of_empty_without_path(tmp_path: Path) -> None:
    from autobot.core.types import ToolCall
    from autobot.tools.access import AccessPolicy, folder_scope_of

    policy = AccessPolicy(tmp_path / "access.json", tmp_path / "ws")
    assert folder_scope_of(policy)(ToolCall(name="empty_trash", arguments={})) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_access.py -k folder_scope_of -v`
Expected: FAIL with `ImportError: cannot import name 'folder_scope_of'`.

- [ ] **Step 3: Implement `folder_scope_of` and wire it**

In `src/autobot/tools/access.py`, add the import at the top:

```python
from autobot.core.types import ToolCall
```

Add near the bottom (after `AccessBroker`):

```python
def folder_scope_of(policy: AccessPolicy) -> Callable[[ToolCall], str]:
    """Build a session-grant scope function keyed on a call's target folder.

    For a path-bearing tool (``delete_file`` etc.) the scope is the resolved parent
    folder, so a session grant means "this action, in this folder". Tools with no
    ``path`` argument (``empty_trash``, ``uninstall_app``) get an empty scope, i.e. a
    tool-name-only grant. Never raises — an unresolvable path yields ``""``.
    """

    def scope_of(call: ToolCall) -> str:
        raw = call.arguments.get("path")
        if isinstance(raw, str) and raw:
            try:
                return str(policy.resolve(raw).parent)
            except Exception:
                return ""
        return ""

    return scope_of
```

In `src/autobot/app.py`, change the gate construction (lines 588-594) to inject the scope function. Add the import with the other access import and pass `scope_of`:

```python
    from autobot.tools.access import folder_scope_of

    gate = PermissionGate(
        registry,
        audit,
        confirmer,
        permission_status=permissions.status_of,
        on_permission_needed=permissions.open_pane,
        scope_of=folder_scope_of(access_policy),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_access.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/access.py src/autobot/app.py tests/unit/test_access.py
git commit -m "feat(gate): scope session grants to the target folder"
```

---

### Task 6: Clear session grants on New Chat

**Files:**
- Modify: `src/autobot/orchestrator/state_machine.py` (`new_chat_session` at `423-441`)
- Modify: `tests/unit/test_state_machine.py` (`_RecordingGate` at `80-92`; add a test)

**Interfaces:**
- Consumes: `PermissionGate.clear_session_grants` (Task 4).
- Produces: `Orchestrator.new_chat_session()` calls `self._gate.clear_session_grants()`.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_state_machine.py`, add a `cleared` flag + method to `_RecordingGate` (class at line 80):

```python
    # inside _RecordingGate.__init__:
    self.cleared = 0

    def clear_session_grants(self) -> None:
        self.cleared += 1
```

Add a test:

```python
def test_new_chat_session_clears_session_grants() -> None:
    gate = _RecordingGate()
    orch = _orchestrator("unused", gate)
    orch.new_chat_session()
    assert gate.cleared == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_state_machine.py::test_new_chat_session_clears_session_grants -v`
Expected: FAIL (`gate.cleared` is 0 — orchestrator doesn't call it yet).

- [ ] **Step 3: Call `clear_session_grants` in `new_chat_session`**

In `src/autobot/orchestrator/state_machine.py`, inside `new_chat_session` under the turn lock (after `self._sm.reset(State.IDLE)`), add:

```python
            # Forget "allow this session" grants so a fresh chat re-confirms actions.
            self._gate.clear_session_grants()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_state_machine.py -v`
Expected: PASS (new test + existing `test_new_chat_session_*` tests, now that `_RecordingGate` has the method).

- [ ] **Step 5: Commit**

```bash
git add src/autobot/orchestrator/state_machine.py tests/unit/test_state_machine.py
git commit -m "feat(gate): reset session grants on New Chat"
```

---

### Task 7: `VoiceConfirmer.confirm_action` + voice "session" cue

**Files:**
- Modify: `src/autobot/tools/confirm.py` (add `_GRANT_OPTIONS`, `_SESSION_CUES`, `_session_cue`; extend `choose` `171-225`; add `confirm_action`)
- Modify: `tests/unit/test_confirm.py`

**Interfaces:**
- Consumes: the existing `VoiceConfirmer.choose` machinery.
- Produces:
  - `VoiceConfirmer.confirm_action(prompt, kind="danger") -> str` = `self.choose(prompt, _GRANT_OPTIONS, kind, default="once")`.
  - `choose` returns `"session"` when the spoken answer contains a session cue **and** a `"session"` option value is present; otherwise unchanged (plain yes → default, no/silence/timeout → "").

- [ ] **Step 1: Write the failing tests**

The file already has `_voice(answers) -> (VoiceConfirmer, _Flow)` (voice, scripted
`listen`) and `_LEVELS` (the read/write folder options). Append to
`tests/unit/test_confirm.py`:

```python
_GRANT = [
    {"label": "Allow once", "value": "once"},
    {"label": "Allow this session", "value": "session"},
]


def test_confirm_action_click_session_in_chat() -> None:
    polls = {"n": 0}

    def poll() -> str | None:
        polls["n"] += 1
        return "session" if polls["n"] >= 2 else None  # drain, then the picked value

    c = VoiceConfirmer(
        speak=lambda _s: None,
        listen=lambda _t: "",
        on_show=lambda p, k, o: None,
        poll_answer=poll,
        is_chat=lambda: True,
        timeout_s=10.0,
        clock=lambda: 0.0,
        sleep=lambda _s: None,
    )
    assert c.confirm_action("Delete it?") == "session"


def test_confirm_action_plain_yes_is_once_by_voice() -> None:
    c, _ = _voice(["yes"])
    assert c.confirm_action("Delete it?") == "once"


def test_choose_voice_session_cue_returns_session() -> None:
    c, _ = _voice(["yes for all this session"])
    assert c.choose("Delete it?", _GRANT, "danger", "once") == "session"


def test_choose_session_cue_ignored_without_session_option() -> None:
    # Folder-grant options have no "session" value -> a session cue can't invent one;
    # a plain yes still maps to the least-privilege default.
    c, _ = _voice(["yes for all of them"])
    assert c.choose("Let Jack in?", _LEVELS, "read", "read") == "read"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_confirm.py -k "confirm_action or session_cue" -v`
Expected: FAIL (`confirm_action` missing; cue not recognized).

- [ ] **Step 3: Implement the cue + `confirm_action`**

In `src/autobot/tools/confirm.py`, add module-level constants near the other word sets (after `_YES_PHRASES`, ~line 87):

```python
_GRANT_OPTIONS: list[dict[str, str]] = [
    {"label": "Allow once", "value": "once"},
    {"label": "Allow this session", "value": "session"},
]
# Spoken cues that mean "grant this for the rest of the session", not just once.
_SESSION_CUES = ("for all", "this session", "every time", "always", "don't ask", "dont ask")


def _session_cue(text: str) -> bool:
    """Whether a spoken answer asks to remember the grant for the session."""
    lowered = text.lower()
    return any(cue in lowered for cue in _SESSION_CUES)
```

In `choose`, inside the voice branch, detect the cue **before** the yes/no parse. Locate
these lines (currently ~209-217):

```python
                text = self._listen(chunk)
                if not text.strip():
                    continue
                ans = parse_confirmation(text)
                if ans is True:
                    return default
                if ans is False:
                    return ""
```

and change to:

```python
                text = self._listen(chunk)
                if not text.strip():
                    continue
                if "session" in valid and _session_cue(text) and parse_confirmation(text) is not False:
                    return "session"
                ans = parse_confirmation(text)
                if ans is True:
                    return default
                if ans is False:
                    return ""
```

Add the `confirm_action` method to `VoiceConfirmer` (after `confirm`, end of class):

```python
    def confirm_action(self, prompt: str, kind: str = "danger") -> str:
        """Confirm a gated action, offering an "Allow this session" grant.

        Reuses :meth:`choose` (card + inbox + voice), so a click picks a button and a
        spoken plain "yes" grants ``"once"`` while a session cue ("for all", "this
        session") grants ``"session"``. Returns "" on cancel / silence / timeout.
        """
        return self.choose(prompt, _GRANT_OPTIONS, kind, default="once")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_confirm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/confirm.py tests/unit/test_confirm.py
git commit -m "feat(confirm): voice 'allow this session' cue + confirm_action"
```

---

### Task 8: Confirm card renders option buttons

**Files:**
- Modify: `ui/orb/components/confirm-card/confirm-card.js` (`showConfirm` `11-66`)
- Modify: `ui/orb/components/confirm-card/confirm-card.test.js` (`with options` test at `27-33`)

**Interfaces:**
- Produces: when `options` are provided, the card renders a **Cancel** button plus one button per option (each with `data-v="<option.value>"`) instead of a `<select>` dropdown; clicking posts `{value: <that value>}`. Cancel posts `{value: "no"}`. The no-options (plain yes/no) path is unchanged.

- [ ] **Step 1: Update the options test to expect buttons**

In `ui/orb/components/confirm-card/confirm-card.test.js`, replace the `with options` test (lines 27-33):

```javascript
it("with options, each option renders a button that posts its value", () => {
  const log = makeLog();
  showConfirm(log, "pick", "read", [{ value: "once", label: "Allow once" }, { value: "session", label: "Allow this session" }]);
  const card = log.querySelector("#confirm-card");
  expect(card.querySelector('[data-v="session"]').textContent).toBe("Allow this session");
  card.querySelector('[data-v="once"]').click();
  expect(daemon.confirm).toHaveBeenCalledWith({ value: "once" });
});

it("options card has a Cancel button that posts no", () => {
  const log = makeLog();
  showConfirm(log, "pick", "danger", [{ value: "once", label: "Allow once" }, { value: "session", label: "Session" }]);
  log.querySelector('[data-v="no"]').click();
  expect(daemon.confirm).toHaveBeenCalledWith({ value: "no" });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ui && npx vitest run orb/components/confirm-card/confirm-card.test.js`
Expected: FAIL (the card still builds a `#confSel` dropdown, no `[data-v="once"]`).

- [ ] **Step 3: Render options as buttons**

In `ui/orb/components/confirm-card/confirm-card.js`, the consts `head`, `yes`, `yesCls`,
`no` (lines 17-20) stay as-is. Replace **only lines 21-35** (the `card.innerHTML` that
builds the row inline, plus the `<select>`/`selEl` block) with a `hasOpts` branch that
builds the row programmatically:

```javascript
  card.innerHTML = '<div class="h"></div><div class="b"></div>';
  card.querySelector(".h").textContent = head;
  card.querySelector(".b").textContent = prompt || "Do you want me to go ahead with this?";
  const row = document.createElement("div"); row.className = "row";
  if (hasOpts) {
    // One button per option (last is the primary), plus Cancel — clearer than a dropdown.
    const cancel = document.createElement("button"); cancel.className = "btn"; cancel.setAttribute("data-v", "no"); cancel.textContent = no;
    row.appendChild(cancel);
    options.forEach((o, i) => {
      const b = document.createElement("button");
      b.className = i === options.length - 1 ? yesCls : "btn";
      b.setAttribute("data-v", o.value); b.textContent = o.label;
      row.appendChild(b);
    });
  } else {
    const noBtn = document.createElement("button"); noBtn.className = "btn"; noBtn.setAttribute("data-v", "no"); noBtn.textContent = no;
    const yesBtn = document.createElement("button"); yesBtn.className = yesCls; yesBtn.setAttribute("data-v", "yes"); yesBtn.textContent = yes;
    row.appendChild(noBtn); row.appendChild(yesBtn);
  }
  card.appendChild(row);
```

(Do **not** re-declare `head`/`yes`/`yesCls`/`no` — they're already defined at lines
17-20, and `hasOpts` at line 15.)

Then simplify the click handler (old lines 56-63) — `data-v` is already the value to post,
so the `selEl` lookup is gone:

```javascript
  card.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      daemon.confirm({ value: b.getAttribute("data-v") });
      card.remove();
    });
  });
```

> The network disclosure block (`kind === "network"`, old lines 37-55) is **unchanged**
> and still works: it does `card.querySelector(".row")` and inserts the `.kv` rows before
> it, and `row` carries class `row` and is already appended to the card before that block
> runs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ui && npx vitest run orb/components/confirm-card/confirm-card.test.js`
Expected: PASS (button tests + the unchanged danger/network tests).

- [ ] **Step 5: Commit**

```bash
git add ui/orb/components/confirm-card/confirm-card.js ui/orb/components/confirm-card/confirm-card.test.js
git commit -m "feat(ui): render confirm options as buttons (Once / This session / Cancel)"
```

---

### Task 9: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Python checks**

Run: `cd /Users/mohamedjakkariyar/work/browserstack/autobot && make check`
Expected: ruff, ruff-format, mypy strict, and pytest all PASS.

- [ ] **Step 2: UI checks**

Run: `cd ui && npx vitest run`
Expected: all suites PASS (confirm-card especially).

- [ ] **Step 3: Manual smoke (optional, needs a running daemon + Ollama)**

Grant a folder, then in chat ask to delete two files in it. Expect: the first delete
shows `[ Cancel ] [ Allow once ] [ Allow this session ]`; choosing **Allow this session**
deletes the rest without another card. Ask to delete a file in a *different* folder →
a card appears again. **New chat**, repeat → card appears again. Delete a non-existent
file → the reply reports it was NOT deleted (no false success).

- [ ] **Step 4: Commit anything outstanding**

```bash
git status   # should be clean; if make format changed files, commit them
```

---

## Self-review notes

- **Spec coverage:** Part 1 (session grants) → Tasks 4–6; Part 2 (confirmer + card) →
  Tasks 3, 7, 8; network exclusion → Task 4 test + `execute` branch; Part 3 (truthful
  deletes) → Tasks 1–2. Testing section → per-task tests + Task 9.
- **Type consistency:** `confirm_action(prompt, kind="danger") -> str`, `scope_of:
  Callable[[ToolCall], str] | None`, `clear_session_grants() -> None`, `folder_scope_of(
  policy) -> Callable[[ToolCall], str]`, `ToolError(Exception)` — used identically
  across tasks.
- **Grant return contract:** `""` = cancel, `"session"` = remember + proceed, any other
  non-empty (i.e. `"once"`) = proceed without remembering. The gate only special-cases
  `"session"` and empty, so a legacy `bool→"once"` fallback is safe.
