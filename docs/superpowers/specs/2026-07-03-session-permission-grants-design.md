# Session-scoped permission grants (+ truthful delete results)

**Issue:** [#40](https://github.com/mdjakkariya/jacky/issues/40) — cleanup of desktop
files asks for confirmation once per file, and files reported deleted still remain.

## Problem

Two defects, both surfaced by "clean up my desktop":

1. **Confirmation fatigue.** Every destructive tool call is confirmed independently.
   `PermissionGate.execute` calls `confirmer.confirm(...)` for each call at or above
   `Risk.DESTRUCTIVE` (and for network-writes) with **no memory** of prior decisions.
   Deleting 10 files → 10 cards. There is no "allow once / allow this session".
2. **Silent-failure over-claim.** `delete_file` returns `"not found: X"` as an ordinary
   string. `ToolRegistry.dispatch` only sets `ok=False` on an *exception*, so a
   not-found (or access-denied) delete is recorded `ok=True`. Both the audit log and
   the model read it as success — the model reports "deleted 6 files" while they remain.

The two interact: auto-approving a session (defect 1's fix) would *hide* silently
failing deletes (defect 2) and make the UX worse. They must land together.

## Prior art in the codebase

- **Scope layer already remembers.** `AccessPolicy` / `AccessBroker` (`tools/access.py`)
  grant folder access once, persist it (`~/.autobot/access.json`), and use a
  multi-option choice card (`choose()` with Read-only / Read & write). The gap is only
  the **risk layer** (the gate), which re-asks every time.
- **The card transport already carries options.** `Confirmer.choose(prompt, options,
  kind, default) -> str` flows end-to-end: gate → `on_confirm` → daemon bus →
  `confirm` event → `confirm-card.js` (which renders `m.options`) → `/confirm` →
  `ConfirmInbox`. Adding an "Allow this session" button needs **no new transport**.

## Non-goals

- No on-disk / cross-restart persistence of destructive grants ("Always allow"). Session
  scope only (cleared on app restart and on New Chat). Revisit later if wanted.
- No batch tool (`delete_files([...])`). The general session-grant primitive covers the
  reported case; a batch tool stays a possible future add.
- No change to the folder *scope* layer (`AccessPolicy`); it already behaves correctly.

## Design

### Part 1 — Session grants in the gate

`PermissionGate` gains an in-memory grant set, checked before prompting.

```python
class PermissionGate:
    def __init__(self, ..., scope_of: Callable[[ToolCall], str] | None = None):
        ...
        self._scope_of = scope_of
        self._session_grants: set[str] = set()

    def _grant_key(self, call: ToolCall) -> str:
        scope = self._scope_of(call) if self._scope_of else ""
        return f"{call.name}|{scope}"

    def clear_session_grants(self) -> None:
        self._session_grants.clear()
```

In `execute`, when a call needs confirmation:

1. If `_grant_key(call) in _session_grants` → skip the card, dispatch, and audit with
   `detail` noting it was a session grant (so the audit trail still records every action).
2. Otherwise call `choose(...)` (see Part 2). On `"session"`, add the key to
   `_session_grants` before dispatching. On `"once"`, dispatch without remembering. On
   `""` (cancel), the existing decline path is unchanged.

**Grant key = `f"{tool}|{scope}"`.** `scope` is derived by an injected
`scope_of(call) -> str`, wired in `app.py::build()` (same pattern as
`permission_status` / `on_permission_needed`). Default (`None`) → tool-only scope.

`scope_of` resolves the **target folder** for path-bearing tools via the process-wide
`access.active_policy()`:

```python
def make_scope_of(policy: AccessPolicy) -> Callable[[ToolCall], str]:
    def scope_of(call: ToolCall) -> str:
        raw = call.arguments.get("path")          # delete_file's target
        if isinstance(raw, str) and raw:
            try:
                return str(policy.resolve(raw).parent)
            except Exception:
                return ""
        return ""                                  # folderless tool → tool-only scope
    return scope_of
```

Result: `delete_file|/Users/x/Desktop` is granted for the session after the first
approval; a later `delete_file` in `~/Documents` (different key) still confirms.
`empty_trash` / `uninstall_app` (no `path` arg) get tool-only scope.

**Lifetime.** In-memory → cleared on app restart. `Orchestrator.new_chat_session()`
calls `gate.clear_session_grants()` so "New chat" also resets grants.

### Part 2 — Confirmer & card

A new **tri-state** confirmer entry point expresses the grant decision that a bool
`confirm()` can't:

```python
def confirm_action(self, prompt: str, kind: str = "danger") -> str:
    """Return "once" (proceed), "session" (proceed + remember), or "" (cancel)."""
```

Added to the `Confirmer` protocol and implemented by `TerminalConfirmer`,
`AlwaysAllow` (→ `"once"`), `AlwaysDeny` (→ `""`), and `VoiceConfirmer`. The gate calls
it via `getattr` fallback so any confirmer that only implements `confirm()` still works
unchanged (`bool → "once"/""`), which keeps every existing gate test green:

```python
def _confirm_action(self, prompt: str, kind: str) -> str:
    fn = getattr(self._confirmer, "confirm_action", None)
    if callable(fn):
        return fn(prompt, kind)
    return "once" if self._confirmer.confirm(prompt, kind) else ""
```

`VoiceConfirmer.confirm_action` reuses the existing `choose()` machinery internally
(so it's DRY — one card/inbox/voice flow), passing two grant options with a
least-privilege default:

```python
_GRANT_OPTIONS = [
    {"label": "Allow once", "value": "once"},
    {"label": "Allow this session", "value": "session"},
]
# VoiceConfirmer.confirm_action -> self.choose(prompt, _GRANT_OPTIONS, kind, default="once")
```

**Network / egress actions are deliberately excluded** from session grants. An
off-device send is the privacy-critical moment the project's disclosed-egress guarantee
exists to protect (CLAUDE.md constraint #1), so `spec.network` calls keep the current
**per-call `confirm()`** — every off-device send is confirmed, always. Only local
destructive tools call `confirm_action` and can offer "Allow this session".

**Card UI (`ui/orb/components/confirm-card/confirm-card.js`).** Today `options` render
as a `<select>` dropdown plus Yes/No. Change: when `options` are present, render each
option as an **explicit button** alongside a **Cancel** button — e.g.
`[ Cancel ] [ Allow once ] [ Allow this session ]` — each posting its own `value`.
The folder-grant card (Read only / Read & write) gets the same button treatment
(cleaner than a dropdown for a two-way choice). Its `daemon.test.js`-style coverage is
updated to the button markup.

**Voice.** `VoiceConfirmer.choose` keeps its safety rules (no-words win, silence/timeout
cancel). Extend its spoken-answer parsing so:
- a plain "yes / proceed / allow / ok" → `default` (`"once"`), and
- session cues → the session value: `{"all", "for all", "this session", "every time",
  "always", "don't ask again", "dont ask"}`.

Minimal (cue words only, no new voice UI). If undesired, the feature degrades to
chat-only cleanly (voice just always returns `once`).

### Part 3 — Truthful delete results (diagnose-first)

**Hypothesis (to confirm by reproduction):** a bare filename passed to `delete_file`
resolves against the workspace cwd (`~/.autobot/workspace`), not the folder the user
meant (e.g. `~/Desktop`); the tool returns `"not found: X"`, which `dispatch` records
as `ok=True`, so the model over-claims success.

Steps:

1. **Reproduce** with a unit test: `delete_file("some.png")` while the active folder is
   not the file's real location → assert the outcome is reported as a **failure**, and
   assert nothing was deleted.
2. **Truthful results:** acting tools (`delete_file`, `move_file`) must surface
   not-found / access-denied as a failure the model cannot read as success — instead of
   a silent `ok=True` string. Establish a small convention for a handler to signal
   failure through the registry (e.g. a sentinel/raise that `dispatch` maps to
   `ok=False`), keeping the "tools never raise out of dispatch" guarantee.
3. **Folder targeting:** confirm whether "clean up my desktop" needs to target the
   Desktop folder (set active folder, or pass absolute paths). Exact fix decided *after*
   step 1 shows the real resolution path — documented here, not pre-baked.

## Data flow

```
model → Orchestrator._execute → PermissionGate.execute
  ├─ network/egress call?  ── yes ─→ confirmer.confirm(...)   (per-call, never remembered)
  ├─ grant key in _session_grants?  ── yes ─→ dispatch (audit: session-grant)
  └─ no → _confirm_action(prompt, kind)   (confirm_action, or confirm() fallback)
            ├─ ""       → decline (unchanged)
            ├─ "once"   → dispatch
            └─ "session"→ _session_grants.add(key); dispatch
New chat → Orchestrator.new_chat_session → gate.clear_session_grants()
```

## Testing

- **Gate (unit):** session grant remembered (2nd matching call skips confirm);
  scope isolation (different folder still confirms); `once` and cancel do *not* grant;
  cancel unchanged; `clear_session_grants` empties the set; grant path is audited;
  **network calls never offer/record a session grant** (always `confirm()` per call).
- **`choose` voice (unit):** session cue → session value; plain yes → `once`; no /
  silence / timeout → cancel.
- **`delete_file` (unit):** not-found / denied reported as failure (reproduces defect 2).
- **Card (JS):** option buttons render and post the correct `value`; Cancel posts `""`.

## Risks & mitigations

- **Blast radius of a session grant.** Bounded by per-action + per-folder scope and by
  clearing on New Chat / restart. No cross-restart persistence.
- **Voice ambiguity.** Least-privilege default (`once`); no-words still cancel; session
  requires an explicit cue.
- **Touching the folder-grant card.** Shared component change — covered by updating its
  tests; behavior (values posted) preserved.
