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
