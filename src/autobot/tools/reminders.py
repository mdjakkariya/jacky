"""macOS Reminders tools — create, list, complete, and delete reminders by voice.

Jack talks to the built-in **Reminders.app** through ``osascript`` (AppleScript),
so everything stays on-device. The four operations map onto the permission gate's
risk levels: listing is ``READ_ONLY``; creating and completing a reminder are
``WRITE`` (reversible — you can delete a new one or un-complete a finished one, so
they run unprompted but audited); **deleting is ``DESTRUCTIVE``**, so the gate
confirms it first. All four send Apple Events to Reminders, so each declares
``requires=AUTOMATION`` — the gate refuses (and opens Settings) when that macOS
permission is known to be missing instead of failing deep in AppleScript.

"remind me at 5" turns into a native Reminders item with a due/alarm date: the
spoken time phrase is parsed by :func:`parse_due` (pure, with an injectable clock)
into an absolute date-time, then handed to AppleScript as numeric components — never
spliced into the script text — so a spoken phrase can't inject AppleScript. If a
phrase can't be parsed, the reminder is still created, just without a time.

Two safeguards keep this from spawning junk or duplicate reminders. **Create won't
fire without a subject:** a missing or generic placeholder title ("remind me in two
minutes" with no "what") is bounced back as a question instead of creating a
"Reminder" item — so the assistant asks first and creates exactly once. **Create is
an upsert:** if an open reminder with the same title already exists, its time is
updated in place rather than adding a second copy. :func:`update_reminder` covers
deliberate edits (rename and/or reschedule an existing reminder).

A ``Runner`` is injected so the command-building and output-formatting logic is
unit-tested without spawning ``osascript`` or touching the real Reminders database.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.permissions import AUTOMATION
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("tools")

# (argv) -> (returncode, combined_output). Injectable so tests don't run osascript.
RunResult = tuple[int, str]
Runner = Callable[[list[str]], RunResult]

_MAX_LIST = 25  # cap how many reminders we read back to the model

# Titles that carry no real subject — the model invented them because the user gave
# a time but never said *what* to be reminded about. We bounce these back as a
# question instead of creating a junk reminder. Matched case-insensitively, with
# surrounding punctuation stripped.
_PLACEHOLDER_TITLES = frozenset(
    {
        "",
        "reminder",
        "a reminder",
        "reminders",
        "remind me",
        "remind",
        "something",
        "this",
        "that",
        "it",
        "note",
        "todo",
        "to do",
    }
)


def _is_placeholder(title: str) -> bool:
    """True when ``title`` has no real subject (so we should ask rather than create)."""
    return title.strip().strip(".!?,").lower() in _PLACEHOLDER_TITLES


# --- natural-language time parsing ---------------------------------------

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
# word -> (hour, minute). These are unambiguous, so they count as an explicit time.
_WORD_TIMES = {
    "noon": (12, 0),
    "midday": (12, 0),
    "midnight": (0, 0),
    "tonight": (19, 0),
    "this morning": (9, 0),
    "this afternoon": (15, 0),
    "this evening": (18, 0),
    "morning": (9, 0),
    "afternoon": (15, 0),
    "evening": (18, 0),
}


def _parse_relative(s: str, now: datetime) -> datetime | None:
    """Handle 'in N minutes/hours/days/weeks' (and 'in a/an …'). Else ``None``."""
    m = re.search(
        r"\bin\s+(\d+|an?)\s+(min(?:ute)?s?|hours?|hrs?|days?|weeks?)\b",
        s,
    )
    if not m:
        return None
    n = 1 if m.group(1) in {"a", "an"} else int(m.group(1))
    unit = m.group(2)
    if unit.startswith(("min",)):
        return now + timedelta(minutes=n)
    if unit.startswith(("hour", "hr")):
        return now + timedelta(hours=n)
    if unit.startswith("day"):
        return now + timedelta(days=n)
    return now + timedelta(weeks=n)


def _parse_date(s: str, now: datetime) -> tuple[datetime, bool]:
    """Resolve the target calendar day. Returns (midnight-of-day, day_was_named)."""
    today = datetime(now.year, now.month, now.day)
    if "day after tomorrow" in s:
        return today + timedelta(days=2), True
    if "tomorrow" in s:
        return today + timedelta(days=1), True
    if "next week" in s:
        return today + timedelta(days=7), True
    for name, idx in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", s):
            ahead = (idx - today.weekday()) % 7
            if ahead == 0:  # a named weekday means the next one, never today
                ahead = 7
            return today + timedelta(days=ahead), True
    if re.search(r"\b(today|tonight|this (?:morning|afternoon|evening))\b", s):
        return today, True
    return today, False


def _parse_time(s: str) -> tuple[int, int, bool] | None:
    """Extract a time of day. Returns (hour, minute, meridiem_explicit) or ``None``."""
    for word, (h, m) in _WORD_TIMES.items():
        if re.search(rf"\b{word}\b", s):
            return h, m, True
    # "5pm", "5 pm", "5:30am", "5p.m."
    mt = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", s)
    if mt:
        hour = int(mt.group(1)) % 12
        if mt.group(3) == "p":
            hour += 12
        return hour, int(mt.group(2) or 0), True
    # "at 5", "at 17:30", "by 9", or a bare "5:30"
    mt = re.search(r"\b(?:at|@|by|around)\s+(\d{1,2})(?::(\d{2}))?\b", s)
    if not mt:
        mt = re.search(r"\b(\d{1,2}):(\d{2})\b", s)  # a colon implies a time
    if mt:
        hour = int(mt.group(1))
        minute = int(mt.group(2) or 0)
        if hour > 23 or minute > 59:
            return None
        # 24-hour style (0, or >12) is unambiguous; 1..12 without am/pm is not.
        explicit = hour == 0 or hour > 12
        return hour, minute, explicit
    return None


def parse_due(phrase: str, now: datetime | None = None) -> datetime | None:
    """Parse a spoken time phrase into an absolute due date-time.

    Handles relative offsets ("in 10 minutes", "in an hour"), named days
    ("tomorrow", "monday", "next week") and times of day ("at 5", "5:30pm",
    "noon", "tonight"), in any combination. ``now`` is injectable so the logic is
    deterministic under test.

    Args:
        phrase: The natural-language time, e.g. "tomorrow at 9am".
        now: The reference moment; defaults to :func:`datetime.now`.

    Returns:
        The resolved :class:`datetime`, or ``None`` if nothing time-like is found.
    """
    now = now or datetime.now()
    s = phrase.strip().lower()
    if not s:
        return None

    relative = _parse_relative(s, now)
    if relative is not None:
        return relative

    base, day_named = _parse_date(s, now)
    tod = _parse_time(s)
    if tod is None:
        if not day_named:
            return None  # neither a day nor a time — not a due date
        hour, minute, explicit = 9, 0, True  # a named day with no time -> 9am
    else:
        hour, minute, explicit = tod

    result = base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Resolve an ambiguous bare hour (1..12, no am/pm).
    if not explicit and hour < 12:
        if day_named:
            # On a named day, lean to the afternoon for the early hours.
            if 1 <= hour <= 6:
                result = result.replace(hour=hour + 12)
        else:
            # Today: pick the next future occurrence (am today, else pm today).
            if result <= now:
                pm = result.replace(hour=hour + 12)
                result = pm if pm > now else result + timedelta(days=1)
    elif not day_named and result <= now:
        # An explicit time today that's already passed rolls to tomorrow.
        result += timedelta(days=1)

    return result


def _friendly(dt: datetime, now: datetime) -> str:
    """Render a due date the way a person would say it (locale-independent)."""
    hour12 = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    clock = f"{hour12}:{dt.minute:02d} {meridiem}"
    delta = (dt.date() - now.date()).days
    if delta == 0:
        day = "today"
    elif delta == 1:
        day = "tomorrow"
    elif 2 <= delta <= 6:
        day = dt.strftime("%A")
    else:
        day = f"{dt.strftime('%b')} {dt.day}"
    return f"{day} at {clock}"


# --- AppleScript (the reminder name/query is always a run-arg, never spliced) ---


# Build an AppleScript `date` from numeric run-args starting at ``base`` (1-indexed):
# year, month, day, hours, minutes. Day is set to 1 first so a month change can't
# overflow (e.g. setting month to Feb while day is 31).
def _date_block(base: int) -> str:
    return (
        "set dd to current date\n"
        "set day of dd to 1\n"
        f"set year of dd to (item {base} of argv as integer)\n"
        f"set month of dd to (item {base + 1} of argv as integer)\n"
        f"set day of dd to (item {base + 2} of argv as integer)\n"
        f"set hours of dd to (item {base + 3} of argv as integer)\n"
        f"set minutes of dd to (item {base + 4} of argv as integer)\n"
        "set seconds of dd to 0\n"
    )


# Upsert by title: if an open reminder with the same name already exists, update it
# in place (so we never add a duplicate); otherwise create one. The optional due
# date arrives as numeric components in items 2..6. Returns "created" or "updated".
# `name is theName` is a case-insensitive exact match (AppleScript ignores case by
# default), so "meeting" updates "Meeting".
_CREATE = (
    "on run argv\n"
    "set theName to item 1 of argv\n"
    'tell application "Reminders"\n'
    "set existing to (reminders whose completed is false and name is theName)\n"
    "if existing is not {} then\n"
    "set r to item 1 of existing\n"
    'set verb to "updated"\n'
    "else\n"
    "set r to (make new reminder with properties {name:theName})\n"
    'set verb to "created"\n'
    "end if\n"
    "if (count of argv) > 1 then\n" + _date_block(2) + "set due date of r to dd\n"
    "set remind me date of r to dd\n"
    "end if\n"
    "return verb\n"
    "end tell\n"
    "end run"
)
# Edit an existing open reminder matched by ``name contains item 1``: optionally
# rename to item 2 (empty = leave) and/or reschedule from numeric items 3..7.
_UPDATE = (
    "on run argv\n"
    "set q to item 1 of argv\n"
    "set newName to item 2 of argv\n"
    'tell application "Reminders"\n'
    "set rs to (reminders whose completed is false and name contains q)\n"
    'if rs is {} then return "NONE"\n'
    "set r to item 1 of rs\n"
    "set oldName to name of r\n"
    'if newName is not "" then set name of r to newName\n'
    "if (count of argv) > 2 then\n" + _date_block(3) + "set due date of r to dd\n"
    "set remind me date of r to dd\n"
    "end if\n"
    'return "OK" & tab & oldName & tab & (count of rs)\n'
    "end tell\n"
    "end run"
)
_LIST = (
    "on run argv\n"
    'set out to ""\n'
    'tell application "Reminders"\n'
    "if (count of argv) is 0 then\n"
    "set rs to (reminders whose completed is false)\n"
    "else\n"
    "set rs to (reminders whose completed is false and name contains (item 1 of argv))\n"
    "end if\n"
    "repeat with r in rs\n"
    'set dtxt to ""\n'
    "try\n"
    "set dd to due date of r\n"
    "if dd is not missing value then set dtxt to (dd as string)\n"
    "end try\n"
    "set out to out & (name of r) & tab & dtxt & linefeed\n"
    "end repeat\n"
    "end tell\n"
    "return out\n"
    "end run"
)


def _action_script(action: str) -> str:
    """AppleScript that finds the first open reminder matching argv[1] and acts on it.

    ``action`` is one of our fixed literals ("set completed of r to true" / "delete
    r"), never user text; the spoken query stays a run-arg.
    """
    return (
        "on run argv\n"
        "set q to item 1 of argv\n"
        'tell application "Reminders"\n'
        "set rs to (reminders whose completed is false and name contains q)\n"
        'if rs is {} then return "NONE"\n'
        "set r to item 1 of rs\n"
        "set nm to name of r\n"
        f"{action}\n"
        'return "OK" & tab & nm & tab & (count of rs)\n'
        "end tell\n"
        "end run"
    )


# Spoken when macOS blocks access — Jack can't flip this switch, only the user can.
_PERMISSION_HINT = (
    "I need permission to use Reminders. macOS should be asking — please allow it for "
    "the app running me under System Settings, Privacy & Security (Reminders, and "
    "Automation). I can't turn that on myself; once you do, just ask me again."
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


class RemindersTools:
    """macOS Reminders operations exposed as gated tools."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = runner or _subprocess_runner

    def _fail(self, out: str, generic: str) -> str:
        """Map a non-zero osascript result to a friendly, actionable message."""
        if _is_permission_error(out):
            return _PERMISSION_HINT
        detail = f" ({out.strip()})" if out.strip() else ""
        return f"{generic}{detail}"

    def list_reminders(self, query: str | None = None) -> str:
        """List open (incomplete) reminders, optionally filtered by name."""
        q = (query or "").strip()
        rc, out = self._run(["osascript", "-e", _LIST, *([q] if q else [])])
        if rc != 0:
            return self._fail(out, "I couldn't read your reminders")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines:
            scope = f" matching “{q}”" if q else ""
            return f"You have no open reminders{scope}."
        items: list[str] = []
        for line in lines[:_MAX_LIST]:
            name, _, due = line.partition("\t")
            name = name.strip()
            due = due.strip()
            items.append(f"{name} (due {due})" if due else name)
        more = len(lines) - _MAX_LIST
        tail = f", and {more} more" if more > 0 else ""
        _log.info("list_reminders count=%d query=%r", len(lines), q)
        return "Your reminders: " + "; ".join(items) + tail + "."

    @staticmethod
    def _date_args(dt: datetime) -> list[str]:
        """The year/month/day/hour/minute run-args for an AppleScript date."""
        return [str(dt.year), str(dt.month), str(dt.day), str(dt.hour), str(dt.minute)]

    def _resolve_due(self, due: str | None, now: datetime | None) -> tuple[datetime | None, str]:
        """Parse ``due`` into (datetime|None, note). The note explains a parse failure."""
        if not due or not due.strip():
            return None, ""
        parsed = parse_due(due, now)
        if parsed is not None:
            return parsed, ""
        return None, f" (I couldn't make sense of the time “{due.strip()}”, so I left it open)"

    def create_reminder(
        self, text: str, due: str | None = None, now: datetime | None = None
    ) -> str:
        """Create a reminder (or update an existing same-named one), with an optional due."""
        title = (text or "").strip()
        if _is_placeholder(title):
            # The user gave a time but no subject — ask, don't invent a reminder.
            return "Sure — what would you like me to remind you about?"
        parsed, note = self._resolve_due(due, now)
        argv = ["osascript", "-e", _CREATE, title]
        if parsed is not None:
            argv += self._date_args(parsed)
        rc, out = self._run(argv)
        if rc != 0:
            return self._fail(out, f"I couldn't create the reminder “{title}”")
        updated = out.strip().lower() == "updated"
        when = f" for {_friendly(parsed, now or datetime.now())}" if parsed is not None else ""
        _log.info(
            "%s_reminder title=%r due=%s",
            "update" if updated else "create",
            title,
            parsed.isoformat() if parsed else None,
        )
        if updated:
            if parsed is not None:
                return f"You already had “{title}” — I updated it{when}.{note}"
            return f"You already have a reminder for “{title}”, so I left it as is."
        return f"Reminder set: “{title}”{when}.{note}"

    def update_reminder(
        self,
        query: str,
        new_text: str | None = None,
        due: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Rename and/or reschedule the first open reminder matching ``query``."""
        q = (query or "").strip()
        if not q:
            return "Which reminder would you like me to change?"
        new_title = (new_text or "").strip()
        parsed, note = self._resolve_due(due, now)
        if not new_title and parsed is None:
            return "What should I change — the wording, the time, or both?"
        argv = ["osascript", "-e", _UPDATE, q, new_title]
        if parsed is not None:
            argv += self._date_args(parsed)
        rc, out = self._run(argv)
        if rc != 0:
            return self._fail(out, "I couldn't update that reminder")
        if out.strip() == "NONE":
            return f"I couldn't find an open reminder matching “{q}”."
        parts = out.strip().split("\t")
        old = parts[1] if len(parts) > 1 else q
        count = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        extra = f" (there were {count} matches; I changed the first)" if count > 1 else ""
        changes: list[str] = []
        if new_title:
            changes.append(f"renamed it to “{new_title}”")
        if parsed is not None:
            changes.append(f"set it for {_friendly(parsed, now or datetime.now())}")
        _log.info("update_reminder old=%r new=%r due=%s", old, new_title or None, parsed)
        return f"Updated “{old}” — {' and '.join(changes)}{extra}.{note}"

    def _act(self, query: str, action: str, verb: str) -> tuple[str, str] | str:
        """Find the first reminder matching ``query`` and run ``action`` on it.

        Returns ``(name, extra)`` on success, or a ready-to-speak ``str`` message
        when the query was empty, nothing matched, or the call failed.
        """
        q = (query or "").strip()
        if not q:
            return f"Which reminder should I {verb}?"
        rc, out = self._run(["osascript", "-e", _action_script(action), q])
        if rc != 0:
            return self._fail(out, f"I couldn't {verb} that reminder")
        if out.strip() == "NONE":
            return f"I couldn't find an open reminder matching “{q}”."
        parts = out.strip().split("\t")
        name = parts[1] if len(parts) > 1 else q
        count = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        extra = f" (there were {count} matches; I took the first)" if count > 1 else ""
        return name, extra

    def complete_reminder(self, query: str) -> str:
        """Mark the first open reminder matching ``query`` as done."""
        result = self._act(query, "set completed of r to true", "complete")
        if isinstance(result, str):  # an error / not-found message
            return result
        name, extra = result
        _log.info("complete_reminder name=%r", name)
        return f"Marked “{name}” as done{extra}."

    def delete_reminder(self, query: str) -> str:
        """Permanently delete the first open reminder matching ``query``."""
        result = self._act(query, "delete r", "delete")
        if isinstance(result, str):
            return result
        name, extra = result
        _log.info("delete_reminder name=%r", name)
        return f"Deleted the reminder “{name}”{extra}."

    def specs(self) -> list[ToolSpec]:
        """Return the tool specs with risk levels for the permission gate."""
        query_param = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words from the reminder's title, e.g. 'call mom'.",
                }
            },
            "required": ["query"],
        }
        return [
            ToolSpec(
                name="create_reminder",
                description=(
                    "Create a reminder in the macOS Reminders app. Cues: 'remind me to "
                    "…', 'remind me at 5 to …', 'set a reminder', 'add a reminder'. Put "
                    "the thing to be reminded about in `text` (e.g. 'call mom'). If the "
                    "user gives a time, pass it verbatim in `due` as a natural phrase — "
                    "'at 5', 'tomorrow at 9am', 'in 10 minutes', 'next monday', 'tonight' "
                    "— do NOT convert it to a date yourself; the tool parses it. Omit "
                    "`due` when no time is mentioned.\n"
                    "IMPORTANT: only call this once you know WHAT to remind about. If the "
                    "user gave a time but no subject (e.g. 'remind me in two minutes'), do "
                    "NOT call this tool — first ask what it's for, then call it once they "
                    "answer. Never invent a placeholder title like 'Reminder'. Calling "
                    "this with a title that matches an existing reminder updates that one "
                    "rather than creating a duplicate; to change wording or time of an "
                    "existing reminder, prefer update_reminder."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "What to be reminded about, e.g. 'call mom'.",
                        },
                        "due": {
                            "type": "string",
                            "description": "Optional spoken time, e.g. 'tomorrow at 9am'.",
                        },
                    },
                    "required": ["text"],
                },
                handler=self.create_reminder,
                risk=Risk.WRITE,
                requires=AUTOMATION,
                ack="Setting that reminder.",
            ),
            ToolSpec(
                name="update_reminder",
                description=(
                    "Change an existing reminder — rename it, reschedule it, or both — "
                    "without creating a new one. Cues: 'change my X reminder to …', 'move "
                    "the X reminder to 6pm', 'rename the X reminder', 'actually make it "
                    "tomorrow'. `query` is words from the current title to find it. Pass "
                    "`new_text` for a new title and/or `due` for a new time (same natural "
                    "phrasing as create_reminder). Use this — not create_reminder — when "
                    "the user is editing something that already exists."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Words from the reminder's current title.",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "Optional new title for the reminder.",
                        },
                        "due": {
                            "type": "string",
                            "description": "Optional new time, e.g. 'tomorrow at 6pm'.",
                        },
                    },
                    "required": ["query"],
                },
                handler=self.update_reminder,
                risk=Risk.WRITE,
                requires=AUTOMATION,
                ack="Updating that reminder.",
            ),
            ToolSpec(
                name="list_reminders",
                description=(
                    "List the user's open (not-yet-done) reminders. Cues: 'what are my "
                    "reminders', 'what do I need to do', 'show my reminders', 'do I have "
                    "any reminders about X'. Pass `query` to filter by title words; omit "
                    "it to list everything open."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Optional title words to filter by.",
                        }
                    },
                    "required": [],
                },
                handler=self.list_reminders,
                risk=Risk.READ_ONLY,
                requires=AUTOMATION,
                ack="Checking your reminders.",
            ),
            ToolSpec(
                name="complete_reminder",
                description=(
                    "Mark a reminder as done / completed / finished. Cues: 'mark X as "
                    "done', 'I finished X', 'check off X', 'complete the reminder about "
                    "X'. Matches the first open reminder whose title contains `query`."
                ),
                parameters=query_param,
                handler=self.complete_reminder,
                risk=Risk.WRITE,
                requires=AUTOMATION,
                ack="Marking that done.",
            ),
            ToolSpec(
                name="delete_reminder",
                description=(
                    "Permanently delete a reminder. Destructive and cannot be undone — "
                    "the user is asked to confirm first. Cues: 'delete the reminder about "
                    "X', 'remove my reminder to X', 'get rid of the X reminder'. To just "
                    "mark something finished, use complete_reminder instead. Matches the "
                    "first open reminder whose title contains `query`."
                ),
                parameters=query_param,
                handler=self.delete_reminder,
                risk=Risk.DESTRUCTIVE,
                requires=AUTOMATION,
                confirm_prompt="🗑️ Delete the matching reminder? This can't be undone.",
                ack="Deleting that reminder.",
            ),
        ]


def register_reminders_tools(
    registry: ToolRegistry, runner: Runner | None = None
) -> RemindersTools:
    """Register the macOS Reminders tools into ``registry``.

    Returns:
        The :class:`RemindersTools` instance, for reference.
    """
    tools = RemindersTools(runner)
    for spec in tools.specs():
        registry.register(spec)
    _log.info("reminders tools registered (create/update/list/complete/delete)")
    return tools
