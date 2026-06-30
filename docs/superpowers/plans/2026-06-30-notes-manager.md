# Notes Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a native-macOS-Notes.app notes manager to Jack — capture (upsert), read, list, re-organize (folders), and clean up notes — all gated and on-device.

**Architecture:** One new module `src/autobot/tools/notes.py` holding a `NotesTools` class with an injected `Runner` (so logic is unit-tested without `osascript`), a set of AppleScript constants (user strings always passed as `on run argv` data, never spliced), and `register_notes_tools(registry, runner=None)`. Wired into `app.py::build()` behind a new `allow_notes` setting. This is a near-twin of the existing `reminders.py`.

**Tech Stack:** Python 3.11+, `osascript`/AppleScript (already used by `reminders.py`/`apps.py`), pytest. No new dependencies.

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` at the top of every module.
- mypy runs in **strict** mode — keep it green. Full type hints on every function.
- Line length 100. Formatting/import order owned by `ruff` — run `make format`, never hand-format.
- Google-style docstrings on public modules, classes, and functions (ruff `D` rules; tests are exempt).
- Tools **return strings and never raise out of their handler** — errors become a friendly message string.
- **On-device only.** No new off-device calls. Native Notes.app via `osascript` only.
- **Injection safety:** every user-supplied string (`title`, `text`, `folder`, `query`) is passed as an `on run argv` item and concatenated *inside* the AppleScript as data — **never** spliced into the script source.
- **Permission gate is mandatory:** every tool declares a `Risk` and `requires=AUTOMATION`.
- Component logger: `_log = get_logger("notes")` → `[notes]` tag. Log seam events at INFO; no hot-loop logging.
- `make check` (ruff, ruff-format, mypy strict, pytest) must pass before each commit. AppleScript-touching behavior is verified manually against the real Notes.app (unit tests inject the runner).
- Commits use Conventional Commits (`feat:`, `test:`, `chore:` …). **No `Co-Authored-By` trailer.**

**Reference spec:** `docs/superpowers/specs/2026-06-30-notes-tool-design.md`
**Reference implementation to mirror:** `src/autobot/tools/reminders.py` and `tests/unit/test_reminders.py`.

---

## File Structure

- **Create:** `src/autobot/tools/notes.py` — `NotesTools` class, AppleScript constants, `_subprocess_runner`, permission-error helpers, `register_notes_tools`.
- **Create:** `tests/unit/test_notes.py` — fake-runner unit tests for every handler + registration.
- **Modify:** `src/autobot/config.py` — add `allow_notes: bool = True` next to `allow_reminders`.
- **Modify:** `src/autobot/app.py` — add the `allow_notes` registration block in `build()`, mirroring the `allow_reminders` block.

Each task ends with a green `make check` and a commit.

---

### Task 1: Module scaffold + `note` upsert (capture) + config + wiring

The shippable MVP — the original issue #5 scope. After this task, Jack can capture a note by voice/chat: a new title creates a note, a known title appends to it, with an optional target folder.

**Files:**
- Create: `src/autobot/tools/notes.py`
- Create: `tests/unit/test_notes.py`
- Modify: `src/autobot/config.py` (add `allow_notes: bool = True` after line 217, the `allow_reminders` line)
- Modify: `src/autobot/app.py` (add registration block after the `allow_reminders` block, ~line 439)

**Interfaces:**
- Produces:
  - `class NotesTools.__init__(self, runner: Runner | None = None)` where `Runner = Callable[[list[str]], tuple[int, str]]`.
  - `NotesTools.note(self, title: str, text: str, folder: str | None = None) -> str`
  - `NotesTools.specs(self) -> list[ToolSpec]` (returns the `note` spec in this task; later tasks append more).
  - `register_notes_tools(registry: ToolRegistry, runner: Runner | None = None) -> NotesTools`
  - Module constant `_UPSERT: str` (the AppleScript).
- Consumes: `autobot.core.types.Risk`, `autobot.permissions.AUTOMATION`, `autobot.tools.registry.{ToolRegistry, ToolSpec}`, `autobot.logging_setup.get_logger`.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_notes.py`)

```python
"""Tests for the macOS Notes tools (osascript via an injected runner)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.tools.notes import NotesTools, register_notes_tools
from autobot.tools.registry import ToolRegistry


class FakeRunner:
    """Records the argv it was called with and returns a canned (rc, output)."""

    def __init__(self, result: tuple[int, str] = (0, "")) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        return self.result


# --- note() upsert -------------------------------------------------------


def test_note_create_branch_passes_title_text_and_empty_folder() -> None:
    runner = FakeRunner((0, "created"))
    tools = NotesTools(runner)
    msg = tools.note("buy milk", "2% gallon")
    argv = runner.calls[-1]
    assert argv[0] == "osascript" and argv[1] == "-e"
    # title, text, folder("") are the three trailing data args.
    assert argv[-3:] == ["buy milk", "2% gallon", ""]
    assert "buy milk" in msg and "Created" in msg


def test_note_append_branch_reports_append() -> None:
    runner = FakeRunner((0, "appended"))
    tools = NotesTools(runner)
    msg = tools.note("shopping", "eggs")
    assert "shopping" in msg
    assert "Added" in msg or "Appended" in msg


def test_note_folder_is_passed_through() -> None:
    runner = FakeRunner((0, "created"))
    tools = NotesTools(runner)
    tools.note("standup", "ship notes tool", folder="Work")
    assert runner.calls[-1][-1] == "Work"


def test_note_blank_title_asks_instead_of_creating() -> None:
    runner = FakeRunner((0, "created"))
    tools = NotesTools(runner)
    msg = tools.note("   ", "something")
    assert "?" in msg          # it asks
    assert runner.calls == []  # and never touches osascript


def test_note_runner_failure_returns_friendly_message_no_raise() -> None:
    runner = FakeRunner((1, "boom"))
    tools = NotesTools(runner)
    msg = tools.note("groceries", "milk")
    assert "groceries" in msg
    assert "boom" in msg  # detail surfaced, no exception


# --- registration --------------------------------------------------------


def test_register_adds_note_tool_as_write() -> None:
    registry = ToolRegistry()
    register_notes_tools(registry, FakeRunner())
    assert registry.get("note").risk is Risk.WRITE  # type: ignore[union-attr]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_notes.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'autobot.tools.notes'`.

- [ ] **Step 3: Create `src/autobot/tools/notes.py`** with the scaffold + `note` upsert

```python
"""macOS Notes tools — capture, read, organize, and clean up notes by voice.

Jack talks to the built-in **Notes.app** through ``osascript`` (AppleScript), so
everything stays on-device. Operations map onto the permission gate's risk levels:
reading is ``READ_ONLY``; ``note`` (create/append) and ``move_note`` are ``WRITE``
(reversible — run unprompted but audited); **deleting is ``DESTRUCTIVE``**, so the
gate confirms it first. Every tool sends Apple Events to Notes, so each declares
``requires=AUTOMATION`` — the gate refuses (and opens Settings) when that macOS
permission is known to be missing instead of failing deep in AppleScript.

``note`` is an **upsert**: if a note with the same title exists it appends a
paragraph, otherwise it creates one. Notes bodies are HTML and a note's name is
derived from its first body line, so create seeds ``<b>title</b>`` as the heading.

Every user string (title, text, folder, query) is passed to ``osascript`` as an
``on run argv`` item and concatenated *inside* the script as data — never spliced
into the script text — so a spoken note can't inject AppleScript.

A ``Runner`` is injected so the command-building and output-formatting logic is
unit-tested without spawning ``osascript`` or touching the real Notes database.
"""

from __future__ import annotations

from collections.abc import Callable

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.permissions import AUTOMATION
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("notes")

# (argv) -> (returncode, combined_output). Injectable so tests don't run osascript.
RunResult = tuple[int, str]
Runner = Callable[[list[str]], RunResult]

_MAX_LIST = 50  # cap how many notes we read back to the model


# --- AppleScript (title/text/folder/query are always run-args, never spliced) ---

# Upsert by title within an optional folder. argv: 1=title, 2=text, 3=folder ("" =
# default). Appends a paragraph to an existing same-named note, else creates one
# (title as a bold heading so the note's name matches on the next upsert). `name is`
# is a case-insensitive exact match (AppleScript ignores case by default). Returns
# "created" or "appended".
_UPSERT = (
    "on run argv\n"
    "set theName to item 1 of argv\n"
    "set theText to item 2 of argv\n"
    "set theFolder to item 3 of argv\n"
    'tell application "Notes"\n'
    'if theFolder is "" then\n'
    "set existing to (notes whose name is theName)\n"
    "else\n"
    "if not (exists folder theFolder) then make new folder with properties {name:theFolder}\n"
    "set existing to (notes of folder theFolder whose name is theName)\n"
    "end if\n"
    "if existing is not {} then\n"
    "set n to item 1 of existing\n"
    'set body of n to (body of n) & "<div>" & theText & "</div>"\n'
    'set verb to "appended"\n'
    "else\n"
    'set b to "<div><b>" & theName & "</b></div><div>" & theText & "</div>"\n'
    'if theFolder is "" then\n'
    "make new note with properties {body:b}\n"
    "else\n"
    "make new note at folder theFolder with properties {body:b}\n"
    "end if\n"
    'set verb to "created"\n'
    "end if\n"
    "return verb\n"
    "end tell\n"
    "end run"
)

# Spoken when macOS blocks access — Jack can't flip this switch, only the user can.
_PERMISSION_HINT = (
    "I need permission to use Notes. macOS should be asking — please allow it for the "
    "app running me under System Settings, Privacy & Security (Automation). I can't "
    "turn that on myself; once you do, just ask me again."
)


def _is_permission_error(output: str) -> bool:
    """True when an osascript failure is a macOS Automation/privacy denial."""
    low = output.lower()
    return any(
        marker in low
        for marker in (
            "not allowed",
            "not authorized",
            "apple events",
            "doesn't have permission",
            "-1743",
            "-1744",
            "-10004",
        )
    )


def _subprocess_runner(args: list[str]) -> RunResult:
    """Default runner: run ``args`` (no shell) and return (code, combined output)."""
    import subprocess

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


class NotesTools:
    """macOS Notes operations exposed as gated tools."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = runner or _subprocess_runner

    def _fail(self, out: str, generic: str) -> str:
        """Map a non-zero osascript result to a friendly, actionable message."""
        if _is_permission_error(out):
            return _PERMISSION_HINT
        detail = f" ({out.strip()})" if out.strip() else ""
        return f"{generic}{detail}"

    def note(self, title: str, text: str, folder: str | None = None) -> str:
        """Create a note titled ``title`` (or append ``text`` to an existing one)."""
        name = (title or "").strip()
        body = (text or "").strip()
        if not name:
            return "Sure — what should I call this note?"
        if not body:
            return f"What would you like the note “{name}” to say?"
        fld = (folder or "").strip()
        rc, out = self._run(["osascript", "-e", _UPSERT, name, body, fld])
        if rc != 0:
            return self._fail(out, f"I couldn't save the note “{name}”")
        appended = out.strip().lower() == "appended"
        where = f" in {fld}" if fld else ""
        _log.info("note %s title=%r folder=%r", "appended" if appended else "created", name, fld)
        if appended:
            return f"Added that to your “{name}” note."
        return f"Created a note “{name}”{where}."

    def specs(self) -> list[ToolSpec]:
        """Return the tool specs with risk levels for the permission gate."""
        return [
            ToolSpec(
                name="note",
                description=(
                    "Create a note or append to an existing one in the macOS Notes app "
                    "(an upsert). Cues: 'note down …', 'make a note …', 'jot down …', "
                    "'add … to my <name> note'. Put the note's name in `title` and the "
                    "content in `text`. If the user names the note ('my shopping note'), "
                    "use that as `title`; otherwise derive a short 3–5 word title from "
                    "the content. If a note with that title already exists, this APPENDS "
                    "a line to it; otherwise it creates a new one. Pass `folder` only "
                    "when the user names a folder (e.g. 'in my Work folder')."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "The note's name, e.g. 'shopping' or 'buy milk'.",
                        },
                        "text": {
                            "type": "string",
                            "description": "The content to write or append.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Optional folder name to file the note under.",
                        },
                    },
                    "required": ["title", "text"],
                },
                handler=self.note,
                risk=Risk.WRITE,
                requires=AUTOMATION,
                ack="Saving that note.",
            ),
        ]


def register_notes_tools(registry: ToolRegistry, runner: Runner | None = None) -> NotesTools:
    """Register the macOS Notes tools into ``registry``.

    Returns:
        The :class:`NotesTools` instance, for reference.
    """
    tools = NotesTools(runner)
    for spec in tools.specs():
        registry.register(spec)
    _log.info("notes tools registered")
    return tools
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_notes.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Add the `allow_notes` setting** in `src/autobot/config.py`

After the `allow_reminders: bool = True` line (line 217), add:

```python
    allow_notes: bool = True
```

- [ ] **Step 6: Wire registration into `src/autobot/app.py::build()`**

After the `allow_reminders` block (ends ~line 439 with the `print("[reminders] …")` line), add:

```python
    if settings.allow_notes:
        # macOS Notes (capture/read/organize/delete) via osascript; gated like
        # everything else — reads are READ_ONLY, note/move are WRITE, delete
        # confirms (DESTRUCTIVE). On-device.
        from autobot.tools.notes import register_notes_tools

        register_notes_tools(registry)
        log.info("notes ENABLED (note/list/read/move/delete/folders)")
        print("[notes] notes ENABLED — Jack can manage your Notes.")
```

- [ ] **Step 7: Verify the whole suite + types + lint pass**

Run: `make check`
Expected: ruff, ruff-format, mypy strict, pytest all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/autobot/tools/notes.py tests/unit/test_notes.py src/autobot/config.py src/autobot/app.py
git commit -m "feat(notes): note upsert tool over native Notes.app (#5)"
```

---

### Task 2: Read trio — `list_notes`, `read_note`, `list_folders`

READ_ONLY tools that let Jack (and the model) see what notes/folders exist and read one back. These are prerequisites for the organize/cleanup tasks.

**Files:**
- Modify: `src/autobot/tools/notes.py` (add 3 AppleScript constants, 3 handlers, 3 specs)
- Modify: `tests/unit/test_notes.py` (add tests)

**Interfaces:**
- Consumes: `NotesTools`, `_MAX_LIST` from Task 1.
- Produces:
  - `NotesTools.list_notes(self, query: str | None = None, folder: str | None = None) -> str`
  - `NotesTools.read_note(self, title: str) -> str`
  - `NotesTools.list_folders(self) -> str`
  - Module constants `_LIST`, `_READ`, `_FOLDERS`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_notes.py`)

```python
# --- list_notes ----------------------------------------------------------


def test_list_notes_parses_rows_and_passes_filters() -> None:
    out = "shopping\tNotes\tMonday, June 29, 2026 at 9:00:00 AM\n"
    out += "ideas\tWork\tSunday, June 1, 2026 at 1:00:00 PM\n"
    runner = FakeRunner((0, out))
    tools = NotesTools(runner)
    msg = tools.list_notes(query="shop", folder="Notes")
    # folder, query are the two trailing data args.
    assert runner.calls[-1][-2:] == ["Notes", "shop"]
    assert "shopping" in msg and "ideas" in msg


def test_list_notes_empty_reports_none() -> None:
    runner = FakeRunner((0, ""))
    tools = NotesTools(runner)
    assert "no notes" in tools.list_notes().lower()


def test_list_notes_caps_output() -> None:
    out = "".join(f"note{i}\tNotes\twhenever\n" for i in range(80))
    runner = FakeRunner((0, out))
    tools = NotesTools(runner)
    msg = tools.list_notes()
    assert "more" in msg  # the cap surfaced a "+N more" tail


# --- read_note -----------------------------------------------------------


def test_read_note_returns_plaintext() -> None:
    runner = FakeRunner((0, "shopping\nmilk\neggs"))
    tools = NotesTools(runner)
    msg = tools.read_note("shopping")
    assert runner.calls[-1][-1] == "shopping"
    assert "milk" in msg and "eggs" in msg


def test_read_note_missing_says_so() -> None:
    runner = FakeRunner((0, "NONE"))
    tools = NotesTools(runner)
    assert "shopping" in tools.read_note("shopping").lower()


# --- list_folders --------------------------------------------------------


def test_list_folders_parses_names() -> None:
    runner = FakeRunner((0, "Notes\nWork\nRecipes\n"))
    tools = NotesTools(runner)
    msg = tools.list_folders()
    assert "Work" in msg and "Recipes" in msg


def test_read_tools_register_as_read_only() -> None:
    registry = ToolRegistry()
    register_notes_tools(registry, FakeRunner())
    assert registry.get("list_notes").risk is Risk.READ_ONLY  # type: ignore[union-attr]
    assert registry.get("read_note").risk is Risk.READ_ONLY  # type: ignore[union-attr]
    assert registry.get("list_folders").risk is Risk.READ_ONLY  # type: ignore[union-attr]
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/test_notes.py -k "list_notes or read_note or list_folders or read_tools" -q`
Expected: FAIL — `AttributeError: 'NotesTools' object has no attribute 'list_notes'`.

- [ ] **Step 3: Add the AppleScript constants** to `notes.py` (after `_UPSERT`)

```python
# List notes (optionally of one folder, optionally name-filtered). argv: 1=folder
# (""=all), 2=query (""=all). One row per note: name TAB folder TAB modification-date.
_LIST = (
    "on run argv\n"
    "set theFolder to item 1 of argv\n"
    "set theQuery to item 2 of argv\n"
    'set out to ""\n'
    'tell application "Notes"\n'
    'if theFolder is "" then\n'
    "set ns to notes\n"
    "else\n"
    "set ns to notes of folder theFolder\n"
    "end if\n"
    "repeat with n in ns\n"
    "set nm to name of n\n"
    'if theQuery is "" or (nm contains theQuery) then\n'
    "set out to out & nm & tab & (name of container of n) & tab & "
    "(modification date of n as string) & linefeed\n"
    "end if\n"
    "end repeat\n"
    "end tell\n"
    "return out\n"
    "end run"
)

# Read one note's text (no HTML). argv: 1=title. Returns plaintext or "NONE".
_READ = (
    "on run argv\n"
    "set theName to item 1 of argv\n"
    'tell application "Notes"\n'
    "set ns to (notes whose name is theName)\n"
    'if ns is {} then return "NONE"\n'
    "return plaintext of (item 1 of ns)\n"
    "end tell\n"
    "end run"
)

# List folder names in the default account, one per line.
_FOLDERS = (
    "on run argv\n"
    'set out to ""\n'
    'tell application "Notes"\n'
    "repeat with f in folders\n"
    "set out to out & (name of f) & linefeed\n"
    "end repeat\n"
    "end tell\n"
    "return out\n"
    "end run"
)
```

- [ ] **Step 4: Add the handlers** to `NotesTools` (after `note`)

```python
    def list_notes(self, query: str | None = None, folder: str | None = None) -> str:
        """List notes (name + folder + modified date), optionally filtered."""
        q = (query or "").strip()
        fld = (folder or "").strip()
        rc, out = self._run(["osascript", "-e", _LIST, fld, q])
        if rc != 0:
            return self._fail(out, "I couldn't read your notes")
        rows = [ln for ln in out.splitlines() if ln.strip()]
        if not rows:
            scope = f" matching “{q}”" if q else ""
            return f"You have no notes{scope}."
        items: list[str] = []
        for line in rows[:_MAX_LIST]:
            name, _, rest = line.partition("\t")
            fname, _, modified = rest.partition("\t")
            tail = f" [{fname.strip()}, modified {modified.strip()}]" if rest else ""
            items.append(f"{name.strip()}{tail}")
        more = len(rows) - _MAX_LIST
        suffix = f", and {more} more" if more > 0 else ""
        _log.info("notes listed count=%d query=%r folder=%r", len(rows), q, fld)
        return "Your notes: " + "; ".join(items) + suffix + "."

    def read_note(self, title: str) -> str:
        """Read back the plain text of the note named ``title``."""
        name = (title or "").strip()
        if not name:
            return "Which note would you like me to read?"
        rc, out = self._run(["osascript", "-e", _READ, name])
        if rc != 0:
            return self._fail(out, f"I couldn't read the note “{name}”")
        if out.strip() == "NONE":
            return f"I don't see a note called “{name}”."
        _log.info("note read title=%r", name)
        return out.strip()

    def list_folders(self) -> str:
        """List the user's Notes folders."""
        rc, out = self._run(["osascript", "-e", _FOLDERS])
        if rc != 0:
            return self._fail(out, "I couldn't read your Notes folders")
        names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not names:
            return "You have no Notes folders."
        _log.info("folders listed count=%d", len(names))
        return "Your folders: " + ", ".join(names) + "."
```

- [ ] **Step 5: Add the three specs** to the `specs()` return list (before the closing `]`)

```python
            ToolSpec(
                name="list_notes",
                description=(
                    "List the user's notes from the macOS Notes app (name, folder, and "
                    "when each was last modified). Cues: 'what notes do I have', 'show my "
                    "notes', 'list my notes in <folder>', 'which notes are old/stale'. "
                    "Pass `query` to filter by title words and/or `folder` to scope to one "
                    "folder; omit both to list everything. Use this before moving or "
                    "deleting notes so you can show the user exactly which notes match."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Optional title words to filter by.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Optional folder name to scope to.",
                        },
                    },
                    "required": [],
                },
                handler=self.list_notes,
                risk=Risk.READ_ONLY,
                requires=AUTOMATION,
                ack="Checking your notes.",
            ),
            ToolSpec(
                name="read_note",
                description=(
                    "Read back the text of one note by name. Cues: 'what's in my <name> "
                    "note', 'read my <name> note', 'what does my shopping note say'. "
                    "Matches a note whose name is `title` (case-insensitive)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "The note's name, e.g. 'shopping'.",
                        }
                    },
                    "required": ["title"],
                },
                handler=self.read_note,
                risk=Risk.READ_ONLY,
                requires=AUTOMATION,
                ack="Reading that note.",
            ),
            ToolSpec(
                name="list_folders",
                description=(
                    "List the folders in the macOS Notes app. Cues: 'what note folders do "
                    "I have', 'show my Notes folders'. Useful before moving a note so you "
                    "pick an existing folder name."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.list_folders,
                risk=Risk.READ_ONLY,
                requires=AUTOMATION,
                ack="Checking your folders.",
            ),
```

- [ ] **Step 6: Run the new tests, then the full check**

Run: `uv run pytest tests/unit/test_notes.py -q` → PASS
Run: `make check` → PASS

- [ ] **Step 7: Commit**

```bash
git add src/autobot/tools/notes.py tests/unit/test_notes.py
git commit -m "feat(notes): list_notes, read_note, list_folders (read-only) (#5)"
```

---

### Task 3: `move_note` — re-organize into folders

The organize primitive: move a note into a folder, creating the folder if it doesn't exist. WRITE (reversible). Bulk "move all my X notes" is the model composing `list_notes` → N× `move_note`.

**Files:**
- Modify: `src/autobot/tools/notes.py` (add `_MOVE`, `move_note`, spec)
- Modify: `tests/unit/test_notes.py` (add tests)

**Interfaces:**
- Consumes: `NotesTools` from Tasks 1–2.
- Produces: `NotesTools.move_note(self, title: str, folder: str) -> str`; module constant `_MOVE`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_notes.py`)

```python
# --- move_note -----------------------------------------------------------


def test_move_note_existing_folder() -> None:
    runner = FakeRunner((0, "OK\tno"))  # createdFolder = "no"
    tools = NotesTools(runner)
    msg = tools.move_note("pasta recipe", "Recipes")
    assert runner.calls[-1][-2:] == ["pasta recipe", "Recipes"]
    assert "Recipes" in msg
    assert "new folder" not in msg.lower()


def test_move_note_creates_folder_is_announced() -> None:
    runner = FakeRunner((0, "OK\tyes"))  # createdFolder = "yes"
    tools = NotesTools(runner)
    msg = tools.move_note("pasta recipe", "Recipes")
    assert "new folder" in msg.lower() and "Recipes" in msg


def test_move_note_missing_note_says_so() -> None:
    runner = FakeRunner((0, "NONE"))
    tools = NotesTools(runner)
    assert "pasta recipe" in tools.move_note("pasta recipe", "Recipes").lower()


def test_move_note_registers_as_write() -> None:
    registry = ToolRegistry()
    register_notes_tools(registry, FakeRunner())
    assert registry.get("move_note").risk is Risk.WRITE  # type: ignore[union-attr]
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/test_notes.py -k move_note -q`
Expected: FAIL — `AttributeError: 'NotesTools' object has no attribute 'move_note'`.

- [ ] **Step 3: Add the `_MOVE` constant** (after `_FOLDERS`)

```python
# Move the first note named argv[1] into folder argv[2], creating the folder if
# absent. Returns "NONE" if no such note, else "OK" TAB ("yes"|"no") where the flag
# says whether the folder was newly created.
_MOVE = (
    "on run argv\n"
    "set theName to item 1 of argv\n"
    "set theFolder to item 2 of argv\n"
    'tell application "Notes"\n'
    "set ns to (notes whose name is theName)\n"
    'if ns is {} then return "NONE"\n'
    'set createdFolder to "no"\n'
    "if not (exists folder theFolder) then\n"
    "make new folder with properties {name:theFolder}\n"
    'set createdFolder to "yes"\n'
    "end if\n"
    "move (item 1 of ns) to folder theFolder\n"
    'return "OK" & tab & createdFolder\n'
    "end tell\n"
    "end run"
)
```

- [ ] **Step 4: Add the `move_note` handler** (after `read_note`)

```python
    def move_note(self, title: str, folder: str) -> str:
        """Move the note named ``title`` into ``folder`` (creating it if needed)."""
        name = (title or "").strip()
        fld = (folder or "").strip()
        if not name or not fld:
            return "Tell me which note to move and which folder to put it in."
        rc, out = self._run(["osascript", "-e", _MOVE, name, fld])
        if rc != 0:
            return self._fail(out, f"I couldn't move the note “{name}”")
        if out.strip() == "NONE":
            return f"I don't see a note called “{name}”."
        created = out.partition("\t")[2].strip() == "yes"
        _log.info("note moved title=%r folder=%r new_folder=%s", name, fld, created)
        if created:
            return f"Moved “{name}” into a new folder “{fld}”."
        return f"Moved “{name}” to “{fld}”."
```

- [ ] **Step 5: Add the `move_note` spec** to `specs()` (before the closing `]`)

```python
            ToolSpec(
                name="move_note",
                description=(
                    "Move a note into a folder in the macOS Notes app, creating the "
                    "folder if it doesn't exist. Cues: 'move my <name> note to <folder>', "
                    "'file the <name> note under <folder>', 'organize … into …'. Matches "
                    "the note whose name is `title`. To move several notes, call this once "
                    "per note (use list_notes first to find them)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "The note's name to move.",
                        },
                        "folder": {
                            "type": "string",
                            "description": "Destination folder name (created if missing).",
                        },
                    },
                    "required": ["title", "folder"],
                },
                handler=self.move_note,
                risk=Risk.WRITE,
                requires=AUTOMATION,
                ack="Moving that note.",
            ),
```

- [ ] **Step 6: Run the new tests, then the full check**

Run: `uv run pytest tests/unit/test_notes.py -q` → PASS
Run: `make check` → PASS

- [ ] **Step 7: Commit**

```bash
git add src/autobot/tools/notes.py tests/unit/test_notes.py
git commit -m "feat(notes): move_note re-organize into folders (#5)"
```

---

### Task 4: `delete_note` — cleanup (DESTRUCTIVE, multi-match)

Delete every note whose title contains the query (the family-delete the "clean up my X notes" case needs). DESTRUCTIVE → the gate confirms, and deletions land in Notes' "Recently Deleted." The tool's description tells the model to `list_notes` first and read the matching titles to the user before deleting.

**Files:**
- Modify: `src/autobot/tools/notes.py` (add `_DELETE`, `delete_note`, spec)
- Modify: `tests/unit/test_notes.py` (add tests)

**Interfaces:**
- Consumes: `NotesTools` from Tasks 1–3.
- Produces: `NotesTools.delete_note(self, query: str) -> str`; module constant `_DELETE`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_notes.py`)

```python
# --- delete_note ---------------------------------------------------------


def test_delete_note_reports_count_and_titles() -> None:
    runner = FakeRunner((0, "2\tPasta recipe\nCake recipe\n"))
    tools = NotesTools(runner)
    msg = tools.delete_note("recipe")
    assert runner.calls[-1][-1] == "recipe"
    assert "Pasta recipe" in msg and "Cake recipe" in msg
    assert "2" in msg


def test_delete_note_no_match_says_so() -> None:
    runner = FakeRunner((0, "0\t"))
    tools = NotesTools(runner)
    assert "recipe" in tools.delete_note("recipe").lower()


def test_delete_note_blank_query_asks() -> None:
    runner = FakeRunner((0, "0\t"))
    tools = NotesTools(runner)
    msg = tools.delete_note("   ")
    assert "?" in msg
    assert runner.calls == []


def test_delete_note_registers_as_destructive() -> None:
    registry = ToolRegistry()
    register_notes_tools(registry, FakeRunner())
    assert registry.get("delete_note").risk is Risk.DESTRUCTIVE  # type: ignore[union-attr]
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/test_notes.py -k delete_note -q`
Expected: FAIL — `AttributeError: 'NotesTools' object has no attribute 'delete_note'`.

- [ ] **Step 3: Add the `_DELETE` constant** (after `_MOVE`)

```python
# Delete every note whose name contains argv[1]. References are captured into `ns`
# first, then deleted, so the collection isn't mutated mid-iteration. Returns
# count TAB (one deleted title per line).
_DELETE = (
    "on run argv\n"
    "set theQuery to item 1 of argv\n"
    'set out to ""\n'
    "set cnt to 0\n"
    'tell application "Notes"\n'
    "set ns to (notes whose name contains theQuery)\n"
    "repeat with n in ns\n"
    "set out to out & (name of n) & linefeed\n"
    "set cnt to cnt + 1\n"
    "end repeat\n"
    "repeat with n in ns\n"
    "delete n\n"
    "end repeat\n"
    "end tell\n"
    "return (cnt as string) & tab & out\n"
    "end run"
)
```

- [ ] **Step 4: Add the `delete_note` handler** (after `move_note`)

```python
    def delete_note(self, query: str) -> str:
        """Delete every note whose title contains ``query`` (matches reported back)."""
        q = (query or "").strip()
        if not q:
            return "Which notes would you like me to delete?"
        rc, out = self._run(["osascript", "-e", _DELETE, q])
        if rc != 0:
            return self._fail(out, f"I couldn't delete notes matching “{q}”")
        count_str, _, rest = out.partition("\t")
        titles = [ln.strip() for ln in rest.splitlines() if ln.strip()]
        count = len(titles)
        _log.info("notes deleted count=%d query=%r", count, q)
        if count == 0:
            return f"No notes match “{q}”, so I didn't delete anything."
        listed = ", ".join(f"“{t}”" for t in titles)
        plural = "note" if count == 1 else "notes"
        return f"Deleted {count} {plural}: {listed}."
```

- [ ] **Step 5: Add the `delete_note` spec** to `specs()` (before the closing `]`)

```python
            ToolSpec(
                name="delete_note",
                description=(
                    "Permanently delete notes from the macOS Notes app whose title "
                    "contains `query` (they go to Recently Deleted). Destructive — the "
                    "user is asked to confirm first. Cues: 'delete my <name> note', "
                    "'clean up my <X> notes', 'get rid of the notes about Y'. IMPORTANT: "
                    "this can match MULTIPLE notes at once. Before calling it, call "
                    "list_notes with the same words and tell the user exactly which note "
                    "titles match, and only delete after they agree. For 'stale/old' "
                    "notes, use the modified dates from list_notes to decide which."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Title words; every note containing them is deleted.",
                        }
                    },
                    "required": ["query"],
                },
                handler=self.delete_note,
                risk=Risk.DESTRUCTIVE,
                requires=AUTOMATION,
                confirm_prompt="🗑️ Permanently delete the matching notes? They go to Recently Deleted.",
                ack="Deleting those notes.",
            ),
```

- [ ] **Step 6: Run the new tests, then the full check**

Run: `uv run pytest tests/unit/test_notes.py -q` → PASS
Run: `make check` → PASS

- [ ] **Step 7: Commit**

```bash
git add src/autobot/tools/notes.py tests/unit/test_notes.py
git commit -m "feat(notes): delete_note cleanup with confirmation (#5)"
```

---

## Manual verification (after all tasks)

Unit tests inject the runner, so the real AppleScript is verified by hand once (AppleScript syntax for Notes can vary by macOS version — adjust the constants if a step errors against the real app):

1. `make run` with `allow_notes` on; grant Notes Automation when macOS prompts.
2. "Jack, note down: buy milk" → a new note appears in Notes.app.
3. "Add eggs to my buy milk note" → the eggs line is appended.
4. "What notes do I have?" → list includes the note with its folder + modified date.
5. "Move my buy milk note to a Groceries folder" → folder created, note moved.
6. "Read my buy milk note" → text read back.
7. "Delete my buy milk note" → gate confirms; note goes to Recently Deleted.
8. `make logs-grep C=notes` shows the seam events.

---

## Self-Review

**Spec coverage:** `note` upsert (T1) ✓; `list_notes`/`read_note`/`list_folders` (T2) ✓; `move_note` + folder-create (T3) ✓; `delete_note` multi-match + DESTRUCTIVE + conversational-list-first via the tool description (T4) ✓; `allow_notes` setting + `app.py` wiring (T1) ✓; injection-safe argv, `requires=AUTOMATION`, `[notes]` logging (all tasks) ✓; native Notes.app, no new deps, no markdown store, no account selection ✓.

**Placeholder scan:** No TBD/TODO; every step has real code and exact commands.

**Type consistency:** `Runner = Callable[[list[str]], tuple[int, str]]` and handler signatures (`note(title, text, folder=None)`, `list_notes(query=None, folder=None)`, `read_note(title)`, `move_note(title, folder)`, `delete_note(query)`, `list_folders()`) are consistent across the plan and the tests. Tool names (`note`, `list_notes`, `read_note`, `move_note`, `delete_note`, `list_folders`) match between specs, registration, and tests.
