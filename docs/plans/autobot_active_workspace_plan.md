# Active workspace — a current working directory for Jack (design)

Design reference for a new feature (its own GitHub issue + branch — **not** part of the
multi-step-tools work). Records *how* Jack's "active folder" works and *why*. Status lives
in the issue, not here.

> Goal in one line: give Jack a **current working directory** (an "active folder") that
> relative file operations resolve against, that the user can move (by voice/chat or a
> native picker) within granted folders, and that the UI always shows — so file tools are
> meaningful instead of silently dumping into a fixed workspace root.

## 1. Context — the problem

Jack has **two inconsistent file models** today:

- **`filesystem.py`** (`create_file`, `list_files`, `move_file`, `delete_file`) is jailed to a
  single fixed root via the bare `Sandbox` (`sandbox_dir` = `~/.autobot/workspace`). There is
  no location choice — everything lands in the workspace root. **This is the user-visible
  complaint** ("create a file" doesn't ask where; writes to root).
- **`fileio.py`** (`read_file_text`, `write_file`, `edit_file`, `copy_file_to_clipboard`)
  already routes through the grant-based **`AccessPolicy`** + **`AccessBroker`** — deny-by-default
  allowlist, grant-on-first-use via the confirm card, persisted to `~/.autobot/access.json`.

So `create_file` can't leave the workspace while `write_file` can (with a grant) — confusing,
and the file tools aren't meaningful for real work (e.g. a project folder). `docs/plans/
file_access_model.md` already planned "Phase 2: migrate `filesystem.py` off the bare
`Sandbox`"; that migration never happened. This feature does it, and adds the missing piece:
an **active working directory** on top of the allowlist.

## 2. Goals / non-goals

**Goals**

1. A persistent **cwd** (active folder) that relative file ops resolve against; defaults to the
   workspace, survives restarts.
2. Migrate `filesystem.py` onto `AccessPolicy` + cwd (retire the bare `Sandbox`); unify the file
   model. Default behavior (workspace) unchanged — no new prompts until the user leaves it.
3. A `set_working_directory` tool (voice/chat) and a native folder picker (UI), both reusing the
   existing grant-on-first-use card.
4. "Default to cwd, ask when unsure" — relative saves go in the active folder; a save clearly
   unrelated to it prompts for a location.
5. UI: a folder chip + floating modal in the chat drawer showing the cwd, granted folders,
   **Reveal in Finder**, and **Change folder…**; spoken awareness in voice.

**Non-goals (this enables them; separate issues)**

- A full coding agent (running shell commands, a coding edit-loop).
- macOS security-scoped bookmarks / OS-enforced persistence (notarization track).
- Reworking the secret denylist or the risk gate (unchanged).

## 3. Approach — Approach A (cwd on `AccessPolicy`)

Each backend/tool already shares the process-wide `AccessPolicy` (`active_policy()`). Add the
cwd there (cwd and grants are both *scope* state), migrate the jailed tools onto it, and surface
it in the UI. No new standalone component; reuses the grant card, persistence, and the daemon's
`/access`-style endpoints + the event-bus pattern.

### 3.1 Core: cwd on `AccessPolicy`, retire `Sandbox` (`tools/access.py`, `tools/filesystem.py`, `app.py`)

`AccessPolicy` gains:

- **`cwd: Path`** — active base for *relative* paths; defaults to the workspace. Persisted in
  `access.json`, which becomes `{ "cwd": "<path>", "grants": [...] }`.
- **`set_cwd(path) -> Path`** — resolves `path`; refuses if denylisted, missing, or not covered
  by a write grant (raises `NeedsAccessError`/`AccessDeniedError`, so the broker can prompt);
  on success sets + persists `cwd` and fires an optional on-change callback (for the UI event).
- **`resolve(path) -> Path`** — *where*, not *whether*: if `path` is relative, join it onto `cwd`;
  expand `~`, resolve symlinks, collapse `..`. Returns the absolute path. Does **not** check
  grants (so a caller can prompt). Existing **`check(abs_path, write)`** stays the *whether*
  (denylist + allowlist + mode). The allowlist remains the hard boundary — a relative `..` is
  collapsed by `resolve` and then rejected by `check` if it left the grants.

`AccessBroker.ensure(path, write)` becomes cwd-aware by composing them: `abs = policy.resolve(path)`
→ `policy.check(abs, write)`, prompting + granting + retrying on `NeedsAccessError` exactly as
today. So every tool that already calls `broker.ensure` gets cwd-relative resolution for free.

On load, validate the saved `cwd` still exists and is granted; otherwise fall back to the
workspace (never start broken).

**Migrate `filesystem.py`:** `SandboxFilesystem(sandbox)` → a broker-backed version that calls
`broker.ensure(path, write=...)` (the same pattern `fileio.py` uses; the broker now does the
cwd-join via `resolve`), so `create_file "demo.txt"` lands in the **cwd**, and a path outside
grants prompts. The bare
`Sandbox` class is deleted once nothing references it. `sandbox_dir` stays as the
default-workspace / default-cwd location (and the always-granted read-write root). Wire in
`app.py::build()`: filesystem tools receive the broker instead of a `Sandbox`.

### 3.2 Tool + prompting (`tools/`, `llm/ollama_llm.py` SYSTEM_PROMPT)

- **`set_working_directory(path)`** — a WRITE-class tool through the gate; on a not-yet-granted
  folder it triggers the existing grant-on-first-use card (write), then `policy.set_cwd(...)` and
  returns a short confirmation ("Working in `foo` now."). `ToolSpec.description` carries the
  spoken cues ("work in…", "switch to my … project", "use this folder") per the prompt-vs-tool
  convention. Risk = WRITE; gets an `ack` ("Switching to {target}…").
- **cwd in the model's context** — inject `Active folder: <cwd>` into the system context (like the
  memory profile), so "where are you working?" is answerable and relative saves are intentional.
- **Prompt principle** (one general line in `SYSTEM_PROMPT`): *you have an active folder; create
  and edit files there by default; if the user asks to save something clearly unrelated to that
  folder, ask whether to save there or pick another location.* (The "ask when unsure" behavior.)

### 3.3 UI: folder chip + modal + picker + reveal (`core/events.py`, daemon, `ui/orb/chat.html`, `ui/orb-shell/src-tauri/src/main.rs`)

- **`WorkspaceEvent`** on the bus: `{ "type": "workspace", "path": "<full>", "name": "<basename>" }`.
  Published when the cwd changes (via `AccessPolicy`'s on-change callback wired to
  `bus.publish_workspace`) and the current value is sent on WS connect (like `last_state`).
- **chat.html:** a **folder chip** in the header showing `name` → opens a **floating modal**
  (mirrors the existing `.ctx-detail` card) showing the full path, the granted folders,
  **[Reveal in Finder]**, and **[Change folder…]**. Add a `workspace` case to the existing
  `ws.onmessage` `m.type` switch to keep the chip live.
- **[Reveal in Finder]** → the existing `reveal_in_finder(path)` Tauri command (no new code).
- **[Change folder…]** → a new `pick_folder()` Tauri command that runs
  `osascript -e 'POSIX path of (choose folder with prompt "…")'` (shell-out, same style as
  `reveal_in_finder`; no new plugin/dependency), returns the chosen POSIX path (or nothing on
  cancel), then `POST /workspace { path }`.
- **Daemon:** `GET /workspace` (cwd + grants) and `POST /workspace { path }` (→ `run_tool(
  "set_working_directory", {path})` so the grant card + gate + audit all apply), mirroring the
  `/access` endpoints.
- **Voice:** a spoken confirmation on change; "where are you working?" answered from the injected
  context.

## 4. Decisions (locked)

- **Architecture:** Approach A — cwd on `AccessPolicy`; retire the bare `Sandbox`.
- **Persistence:** cwd persists across launches in `access.json`; always shown in the UI.
- **Save targeting:** default to cwd; ask only when the target seems unrelated (soft prompt
  principle + the existing confirm card).
- **Change folder:** both voice/chat (`set_working_directory`) and a native picker; both route
  through the grant card.
- **Picker mechanism:** `osascript` "choose folder" via a small Tauri command (no dialog plugin).
- **Default cwd:** the workspace (`~/.autobot/workspace`) — default behavior unchanged.
- Ships as its **own issue + branch**, off `main`.

## 5. Testing

- **AccessPolicy:** cwd defaults to workspace; `set_cwd` persists + round-trips through
  `access.json`; `resolve` joins a relative path onto cwd; a relative path with `..` can't escape
  the cwd/grants; `set_cwd` to a denylisted / ungranted / missing path is refused; load falls back
  to the workspace when the saved cwd is invalid.
- **Migrated filesystem tools:** `create_file "demo.txt"` lands in the cwd (not the workspace)
  when cwd is set elsewhere; default (no change) still writes to the workspace; an absolute path
  outside grants prompts (broker) and a declined grant returns a friendly message.
- **`set_working_directory` tool:** grants + sets on approve; declined grant leaves cwd unchanged
  with a friendly message.
- **Events:** `publish_workspace` / `WorkspaceEvent.message()` serialization.
- **UI:** manual verification (chip shows the folder, modal lists grants, Reveal opens Finder,
  Change-folder picker sets the cwd) — like the chat step-trace.

## 6. Risks

- **Retiring `Sandbox`** is the riskiest step: confirm nothing else (tools, tests) depends on it;
  `AccessPolicy`'s allowlist must be verified as the equivalent hard boundary (relative `..` can't
  escape, denylist still applies). Covered by the AccessPolicy tests above.
- **Cloud privacy (unchanged):** reading file *contents* into the model still goes to Anthropic in
  cloud mode; cwd/`set_working_directory`/the path are local-only actions and change nothing here.
- **Persisted cwd surprise:** mitigated by always showing the cwd in the UI and the load-time
  validity fallback.
- **"Ask when unsure" is heuristic** (model judgment) — acceptable; worst case is an extra
  confirm or a save into the cwd the user then moves.

## 7. Future (separate issues, enabled by this)

- A coding agent (run commands, multi-file coding loop) on top of the active workspace.
- macOS security-scoped bookmarks for OS-enforced grant persistence (notarization track).
- A Settings "Folders & access" panel surfacing cwd + grants (the `/access` UI already exists in
  part).
