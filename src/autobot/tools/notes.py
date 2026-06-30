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

# List notes (optionally of one folder, optionally name-filtered). argv: 1=folder
# (""=all), 2=query (""=all). One row per note: name TAB folder TAB modification-date.
# We iterate folders and read the folder name from the outer loop rather than asking
# each note for `container of n` — that property fails to coerce to text in Notes
# (error -1700), and the outer-loop name is free anyway.
_LIST = (
    "on run argv\n"
    "set theFolder to item 1 of argv\n"
    "set theQuery to item 2 of argv\n"
    'set out to ""\n'
    'tell application "Notes"\n'
    'if theFolder is "" then\n'
    "set fs to folders\n"
    "else\n"
    "set fs to (folders whose name is theFolder)\n"
    "end if\n"
    "repeat with f in fs\n"
    "set fn to name of f\n"
    'if fn is not "Recently Deleted" then\n'
    "repeat with n in (notes of f)\n"
    "set nm to name of n\n"
    'if theQuery is "" or (nm contains theQuery) then\n'
    "set out to out & nm & tab & fn & tab & (modification date of n as string) & linefeed\n"
    "end if\n"
    "end repeat\n"
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

# List folder names in the default account, one per line (the "Recently Deleted"
# system folder is skipped — it isn't a real organizing destination).
_FOLDERS = (
    "on run argv\n"
    'set out to ""\n'
    'tell application "Notes"\n'
    "repeat with f in folders\n"
    "set fn to name of f\n"
    'if fn is not "Recently Deleted" then set out to out & fn & linefeed\n'
    "end repeat\n"
    "end tell\n"
    "return out\n"
    "end run"
)

# Spoken when macOS blocks access — Jack can't flip this switch, only the user can.
_PERMISSION_HINT = (
    "I need permission to use Notes. macOS should be asking — please allow it for the "
    "app running me under System Settings, Privacy & Security (Automation). I can't "
    "turn that on myself; once you do, just ask me again."
)


def _is_permission_error(output: str) -> bool:
    """Return ``True`` when an osascript failure is a macOS Automation/privacy denial."""
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
    """Run ``args`` (no shell) and return (code, combined output) — the default runner."""
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
        """Store the injected ``runner`` (defaults to the real ``osascript`` runner)."""
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

    def delete_note(self, query: str) -> str:
        """Delete every note whose title contains ``query`` (matches reported back)."""
        q = (query or "").strip()
        if not q:
            return "Which notes would you like me to delete?"
        rc, out = self._run(["osascript", "-e", _DELETE, q])
        if rc != 0:
            return self._fail(out, f"I couldn't delete notes matching “{q}”")
        _, _, rest = out.partition("\t")
        titles = [ln.strip() for ln in rest.splitlines() if ln.strip()]
        count = len(titles)
        _log.info("notes deleted count=%d query=%r", count, q)
        if count == 0:
            return f"No notes match “{q}”, so I didn't delete anything."
        listed = ", ".join(f"“{t}”" for t in titles)
        plural = "note" if count == 1 else "notes"
        return f"Deleted {count} {plural}: {listed}."

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
                    "use that as `title`; otherwise derive a short 3-5 word title from "
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
                confirm_prompt=(
                    "🗑️ Permanently delete the matching notes? They go to Recently Deleted."
                ),
                ack="Deleting those notes.",
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
