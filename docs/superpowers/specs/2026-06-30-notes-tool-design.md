# Notes tool — design

**Issue:** [#5 — Notes tool (create / append a note)](https://github.com/mdjakkariya/jacky/issues/5)
**Date:** 2026-06-30

> **Scope note:** Issue #5 is titled "create / append a note." During design we
> deliberately grew it into a small **notes *manager*** — capture, see, read,
> re-organize (folders), and clean up — because the differentiated, "easier than
> native" value is *organizing* notes (moving many at once, deleting stale ones),
> which is tedious by hand. Issue #5 should be updated to reflect this, or the
> read/organize/delete pieces split into follow-up issues (see "Issue tracking").

## Goal & rationale

Give Jack a low-friction way to **capture, organize, and clean up** notes by voice
or chat. The bar is **"easier than native"**:

- *Capture* — *"note down: buy milk"* must beat unlock → open Notes → find note →
  scroll → type.
- *Organize* — *"move all my recipe notes into a Recipes folder"* must beat
  dragging notes one at a time.
- *Clean up* — *"delete my stale grocery notes"* must beat hunting and trashing
  them by hand.

The backing store is the **native macOS Notes.app** (via `osascript`/AppleScript),
not a private file store. Writing notes to throwaway markdown files would create a
parallel, orphaned store the user has to manage separately from where their notes
actually live — the "waste" outcome we explicitly rejected. Notes.app means notes
show up where the user already keeps them, sync via the user's existing iCloud
choice, and stay on-device. This mirrors the proven pattern in
[`reminders.py`](../../../src/autobot/tools/reminders.py), which talks to the
native Reminders.app the same way (and which already bounces `"note"` as a
reminder title — so notes fill a real, distinct gap).

## Operation set

Six tools in one module, a cohesive Notes surface (comparable to `reminders.py`'s
five). Each earns its place against the goal above.

| Tool | Risk | Purpose |
|---|---|---|
| `note(title, text, folder?)` | WRITE | **Upsert** — create or append (the original #5 scope). |
| `list_notes(query?, folder?)` | READ_ONLY | See note titles + folder + modified-date (capped). The eyes for organize/cleanup, and "what notes do I have." |
| `read_note(title)` | READ_ONLY | Read one note's text back. |
| `move_note(title, folder)` | WRITE | The re-organize primitive; creates the target folder if it doesn't exist. |
| `delete_note(query)` | DESTRUCTIVE | Cleanup; multi-match by title; confirmed. |
| `list_folders()` | READ_ONLY | See folder structure (so the model picks sensible move targets). |

Bulk operations need **no special primitive**: "move all my recipes to Recipes"
is the model composing `list_notes` → N× `move_note` (the existing multi-step-tools
pattern). Same for cleanup: `list_notes` → user confirms → `delete_note`.

### Decisions captured

- **Backing store:** native Notes.app, not a markdown/private store. *(No secrets
  ever touch this path; nothing private is written to disk.)*
- **Capture is a single upsert** (`note`), not separate create/append tools: fewer
  choices for the model, one clear intent. Trade-off accepted — a repeat title
  appends rather than making a second same-named note; that's the desired
  quick-capture behavior.
- **Folders are first-class** (the point of organizing), but **account selection
  is out of scope**: multiple Notes accounts (iCloud + Gmail + On-My-Mac) is rare,
  and folders *within* the default account cover essentially all real organizing.
  Everything operates in the **default account**.
- **No read-aloud-only framing:** `list_notes`/`read_note` exist to *enable
  organizing*, not primarily to narrate notes.

## Title matching

- **Precise ops** (`note` upsert, `read_note`, `move_note`) match on
  **case-insensitive exact title** (AppleScript's default string comparison
  ignores case). If no note matches, `read_note`/`move_note` return a clear
  "no note named X" message (never raise); `note` falls through to *create*.
- **Cleanup** (`delete_note`) matches **every note whose title *contains* the
  query** — the family-delete the cleanup case needs.

The model supplies `title` for `note`: the explicit name when the user named the
note (*"my **shopping** note"*), otherwise a concise 3–5 word title **derived from
the content**. This guidance lives in the tool's `ToolSpec.description` (per repo
convention — never the system prompt). Deriving the title in the model (not in
Python) keeps our code dumb and sidesteps the generic-placeholder-title problem
`reminders.py` has to guard against.

## Delete safety (the part with teeth)

"Clean up my stale notes" is a vague, high-blast-radius instruction; the danger is
deleting the *wrong* set. Mitigations:

1. **`delete_note` is `Risk.DESTRUCTIVE`** → the permission gate confirms (and the
   action is audited). Deletions land in Notes' "Recently Deleted," so they're
   recoverable.
2. **Titles are shown to the user *before* anything is deleted, in conversation.**
   The gate confirms *before* the handler runs, using only the call arguments
   ([`permission.py:170`](../../../src/autobot/tools/permission.py)), so it cannot
   render the *resolved* matched titles in its card. Instead, `delete_note`'s
   description **instructs the model to call `list_notes` first**, read the exact
   matched titles back to the user, and only call `delete_note` after the user
   agrees. The conversation is the surface that shows the titles; the gate's
   DESTRUCTIVE confirm (prompt echoes the query) is the **final backstop**.
3. **"Stale" is not a magic tool concept.** `list_notes` returns modification
   dates; the model surfaces the old ones and the user decides. No staleness
   heuristic is baked into a tool.

> Deferred (out of scope here): extending the `PermissionGate` with a
> `preview(args)->str` hook so a DESTRUCTIVE tool can render the resolved titles
> *in the confirmation card itself*. Cleaner and reusable (trash, file-delete),
> but it modifies security-critical code and belongs in its own issue. The
> conversational flow above is sufficient for now.

## AppleScript mechanics & injection safety

Notes bodies are **HTML**; a note's `name` derives from the first body line; its
`plaintext` property gives the text without markup; `modification date` gives
recency. Per operation:

- **`note` create:** body = `<div><b>{title}</b></div><div>{text}</div>` via
  `make new note` (at the named folder if `folder` given, else the default folder).
- **`note` append:** locate `first note whose name is {title}`, then
  `set body of theNote to (body of theNote) & "<div>{text}</div>"`.
- **`list_notes`:** iterate notes (optionally of one folder / matching `query`),
  return `name`, containing folder, and `modification date`; **cap** the count
  (e.g. 50) so a huge library can't flood the model context.
- **`read_note`:** return `plaintext of (first note whose name is {title})`.
- **`move_note`:** `if not (exists folder named {folder}) then make new folder`,
  then `move theNote to folder {folder}`; the confirmation echoes when a *new*
  folder was created (so a typo is caught).
- **`delete_note`:** collect every `note whose name contains {query}`, delete each;
  return how many were removed.
- **`list_folders`:** return folder names in the default account.

**Injection-safe, exactly like `reminders.py`/`apps.py`:** every user string
(`title`, `text`, `folder`, `query`) is passed as an `on run argv` item and
concatenated *inside* the script as **data**, never spliced into the script
source. A spoken/typed note can't inject AppleScript.

A `Runner` (`list[str] -> (returncode, output)`) is injected into `NotesTools`, so
command-building and output-formatting are unit-tested without spawning `osascript`
or touching the real Notes database.

## Risk, permissions, config

- **Risk levels** as in the table: reads are `READ_ONLY`, `note`/`move_note` are
  `WRITE` (unprompted but audited; reversible), `delete_note` is `DESTRUCTIVE`
  (confirmed).
- **`requires=AUTOMATION`** on every tool — the gate refuses (and opens System
  Settings) when Notes automation is missing, instead of failing deep in
  AppleScript. Same treatment as Reminders / app-control.
- **`allow_notes: bool = True`** — a new field in `config.py` (the single source
  for tunables). Wired in `app.py::build()` in a block mirroring `allow_reminders`:
  lazy import of `register_notes_tools`, an `INFO` seam line, and a `[notes]`
  console line.
- **Privacy note (honest disclosure):** if the **optional cloud LLM** is enabled,
  note *contents* returned by `read_note`/`list_notes` go to Anthropic like any
  other tool result — the same disclosed behavior as listing reminders today, not
  a new off-device path. With the default local LLM, nothing leaves the machine.

## Module layout & wiring

- New module: [`src/autobot/tools/notes.py`](../../../src/autobot/tools/notes.py)
  — `class NotesTools` (injected `Runner`), `specs()`, and
  `register_notes_tools(registry, runner=None)`. Structured as a near-twin of
  `reminders.py`.
- Composition root: add the `allow_notes` block to
  [`src/autobot/app.py`](../../../src/autobot/app.py)`::build()` — the only place
  that names the concrete class.
- No new dependencies (`osascript` is already used by reminders/apps).

## Logging

- Component logger: `_log = get_logger("notes")` → `[notes]` tag (greppable via
  `make logs-grep C=notes`).
- **Seam events (INFO):** `note created/appended title=…`, `note moved title=…
  folder=…`, `notes deleted count=…`, `notes listed count=…`.
- **WARNING** on recoverable failures (osascript non-zero, Notes not running). No
  per-call DEBUG spam (there is no hot loop).

## Testing

`tests/unit/test_notes.py`, using a fake `Runner` — no `osascript`, no Notes.app:

- **`note`:** argv/HTML for create vs append; upsert branch (found→append,
  not-found→create); optional `folder` placement; confirmation strings.
- **`list_notes`:** parses runner output into name/folder/modified rows; `query`
  and `folder` filters; cap enforced.
- **`read_note`:** returns plaintext; "no note named X" when absent.
- **`move_note`:** move-existing vs create-folder-then-move branch; "new folder"
  echo in the confirmation.
- **`delete_note`:** contains multi-match → count formatting; no-match message;
  registered `DESTRUCTIVE` (gate confirm covered by gate tests).
- **`list_folders`:** parses folder names.
- **Error path:** non-zero runner result → failed `ToolResult`, no exception
  escapes `dispatch` (any tool).

## Issue tracking

Per repo convention, planning lives in GitHub Issues, not markdown. Because this
grew past #5's literal "create / append" wording, before implementation we either
(a) update #5's body to the operation set above, or (b) keep #5 as the `note`
upsert and open follow-ups for read/list, move/organize, delete + folders. To be
confirmed with the maintainer.

## Verification

`make check` (ruff, ruff-format, mypy strict, pytest) must pass. The
osascript-touching paths are verified manually against the real Notes.app, since
unit tests inject the runner.

## Out of scope / non-goals

- **No account selection** — always the default Notes account.
- **No private/markdown notes store** — Notes.app only; nothing private on disk.
- **No gate `preview` hook** in this change (titles shown conversationally; see
  Delete safety). Deferred to its own issue.
- **No staleness heuristic** baked into a tool — the model + modification dates +
  user confirmation handle "stale."
