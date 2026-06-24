"""In-memory debug breadcrumbs + a one-shot, shareable debug report.

The rotating file log (``~/.autobot/logs/autobot.log``) is the full trail, but for
a long-running session it grows large and you rarely need all of it — you need the
*recent window of events and every error* leading up to a problem. So we keep a
bounded ring buffer of breadcrumbs in memory (Sentry's model): the last N log
records, plus a separate always-kept tail of warnings/errors, plus a compact
state-transition trace. :func:`build_report` renders these into a single redacted
Markdown blob sized for pasting into a GitHub issue — bounded by the buffer, not by
how long the app ran.

Nothing here leaves the machine; the report is built on demand and handed to the
user to copy or save.
"""

from __future__ import annotations

import logging
import platform
import re
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autobot.config import Settings

_ROOT = "autobot"
_WARN_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})

# Config fields safe to surface in a shared report. A whitelist (not the whole
# settings dict) so a new field can never leak something sensitive by accident.
_REPORT_CONFIG_FIELDS = (
    "input_mode",
    "wake_detector",
    "stt_engine",
    "stt_model",
    "llm_provider",
    "llm_model",
    "anthropic_model",
    "barge_in",
    "aec",
    "end_silence_ms",
    "max_utterance_s",
    "tts_voice",
    "allow_web",
    "allow_memory",
)


@dataclass(frozen=True, slots=True)
class Crumb:
    """One breadcrumb: a single log record reduced to what a report needs."""

    ts: str
    level: str
    component: str
    message: str

    def render(self) -> str:
        """Format as one readable line."""
        return f"{self.ts} {self.level:<7} [{self.component}] {self.message}"


class DiagnosticsBuffer:
    """Bounded in-memory breadcrumbs: recent events, retained errors, state trace."""

    def __init__(self, recent: int = 600, errors: int = 80, states: int = 200) -> None:
        self._recent: deque[Crumb] = deque(maxlen=recent)
        self._errors: deque[Crumb] = deque(maxlen=errors)
        self._states: deque[str] = deque(maxlen=states)
        self._counts: Counter[str] = Counter()
        self._started = datetime.now()
        # Running totals (never wrap, unlike the bounded deques) so a session window
        # can be sliced even after old breadcrumbs have been evicted.
        self._added = 0
        self._errors_added = 0
        self._states_added = 0
        # Marks the start of the current chat session (set by mark_session on "New
        # chat"); defaults to engine start so the first session is the whole run.
        self._session_started = self._started
        self._session_added = 0
        self._session_errors_added = 0
        self._session_states_added = 0

    def add(self, crumb: Crumb) -> None:
        """Record a breadcrumb; warnings/errors are also kept in their own tail."""
        self._recent.append(crumb)
        self._added += 1
        self._counts[crumb.level] += 1
        if crumb.level in _WARN_LEVELS:
            self._errors.append(crumb)
            self._errors_added += 1

    def add_state(self, old: str, new: str) -> None:
        """Append one state transition to the compact sequence trace."""
        self._states.append(f"{datetime.now():%H:%M:%S} {old}→{new}")
        self._states_added += 1

    def mark_session(self) -> None:
        """Mark 'now' as the start of a fresh session (the chat's "New chat").

        The full report still shows everything; only the concise/per-session view
        (:func:`build_dev_report`) slices to breadcrumbs recorded after this point.
        """
        self._session_started = datetime.now()
        self._session_added = self._added
        self._session_errors_added = self._errors_added
        self._session_states_added = self._states_added

    @staticmethod
    def _tail(items: deque[Crumb] | deque[str], since: int, total: int) -> list[Any]:
        """The items added since ``since`` (running total ``total``), capped to buffer."""
        n = min(total - since, len(items))
        return list(items)[-n:] if n > 0 else []

    @property
    def recent(self) -> list[Crumb]:
        """All breadcrumbs currently held (oldest first)."""
        return list(self._recent)

    @property
    def errors(self) -> list[Crumb]:
        """The retained warning/error tail (oldest first)."""
        return list(self._errors)

    @property
    def states(self) -> list[str]:
        """The state-transition trace (oldest first)."""
        return list(self._states)

    def session_recent(self) -> list[Crumb]:
        """Breadcrumbs recorded since the last :meth:`mark_session` (this session)."""
        return self._tail(self._recent, self._session_added, self._added)

    def session_errors(self) -> list[Crumb]:
        """Warnings/errors recorded since the last :meth:`mark_session`."""
        return self._tail(self._errors, self._session_errors_added, self._errors_added)

    def session_states(self) -> list[str]:
        """State transitions recorded since the last :meth:`mark_session`."""
        return self._tail(self._states, self._session_states_added, self._states_added)

    @property
    def counts(self) -> dict[str, int]:
        """Count of breadcrumbs seen per level (since start)."""
        return dict(self._counts)

    @property
    def started(self) -> datetime:
        """When this buffer began collecting (≈ engine start)."""
        return self._started

    @property
    def session_started(self) -> datetime:
        """When the current session began (last :meth:`mark_session`, else start)."""
        return self._session_started


class RingLogHandler(logging.Handler):
    """A logging handler that pushes records into a :class:`DiagnosticsBuffer`."""

    def __init__(self, buffer: DiagnosticsBuffer, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        """Reduce a log record to a breadcrumb (never raises out of logging)."""
        try:
            name = record.name
            component = name[len(_ROOT) + 1 :] if name.startswith(_ROOT + ".") else name
            self._buffer.add(
                Crumb(
                    ts=time.strftime("%H:%M:%S", time.localtime(record.created)),
                    level=record.levelname,
                    component=component,
                    message=record.getMessage(),
                )
            )
        except Exception:  # logging handlers must never propagate an error
            self.handleError(record)


_BUFFER = DiagnosticsBuffer()


def get_buffer() -> DiagnosticsBuffer:
    """The process-wide breadcrumb buffer (attached to the logger in setup_logging)."""
    return _BUFFER


# --- report rendering ------------------------------------------------------

_SK_TOKEN = re.compile(r"sk-[A-Za-z0-9_\-]{12,}")
_HOME = str(Path.home())


def redact(text: str) -> str:
    """Strip secrets/PII so a report is safe to paste publicly.

    Removes API-key-looking tokens and rewrites the user's home directory to ``~``
    (so absolute paths don't leak the account name).
    """
    text = _SK_TOKEN.sub("sk-***REDACTED***", text)
    if _HOME and _HOME != "/":
        text = text.replace(_HOME, "~")
    return text


def _app_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("autobot")
        except PackageNotFoundError:
            return "unknown"
    except Exception:  # version is best-effort metadata
        return "unknown"


def _config_lines(settings: Settings) -> list[str]:
    out: list[str] = []
    for field in _REPORT_CONFIG_FIELDS:
        if hasattr(settings, field):
            out.append(f"{field}: {getattr(settings, field)}")
    return out


def _tail_lines(log_path: Path | None, n: int) -> list[str]:
    if log_path is None:
        return []
    try:
        with log_path.open(encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in fh.readlines()[-n:]]
    except OSError:
        return []


def build_report(
    settings: Settings,
    *,
    buffer: DiagnosticsBuffer | None = None,
    log_path: Path | None = None,
    log_tail: int = 150,
) -> str:
    """Render a compact, redacted Markdown debug report for sharing.

    Bounded by the breadcrumb buffer (recent events + retained errors) plus a tail
    of the on-disk log — so it stays small and focused regardless of session length.
    """
    buf = buffer or get_buffer()
    now = datetime.now()
    counts = buf.counts
    count_str = " · ".join(f"{lvl}={n}" for lvl, n in sorted(counts.items())) or "none"

    parts: list[str] = []
    parts.append("# Jack debug report")
    parts.append(f"_generated {now:%Y-%m-%d %H:%M:%S}_\n")
    parts.append(
        f"**App** v{_app_version()} · {platform.platform()} · "
        f"{platform.machine()} · Python {platform.python_version()}\n"
    )

    parts.append("## Config")
    parts.append("```\n" + "\n".join(_config_lines(settings)) + "\n```\n")

    parts.append("## Summary")
    parts.append(
        f"events {count_str} · states tracked {len(buf.states)} · since {buf.started:%H:%M:%S}\n"
    )

    parts.append("## State sequence")
    states = buf.states
    parts.append("```\n" + ("\n".join(states) if states else "(none)") + "\n```\n")

    parts.append("## Errors & warnings")
    errs = [c.render() for c in buf.errors]
    parts.append("```\n" + ("\n".join(errs) if errs else "(none)") + "\n```\n")

    parts.append("## Recent events")
    recent = [c.render() for c in buf.recent]
    parts.append(f"<details><summary>last {len(recent)} events</summary>\n")
    parts.append("```\n" + ("\n".join(recent) if recent else "(none)") + "\n```")
    parts.append("</details>\n")

    tail = _tail_lines(log_path, log_tail)
    parts.append("## Log tail")
    parts.append(f"<details><summary>last {len(tail)} log lines</summary>\n")
    parts.append("```\n" + ("\n".join(tail) if tail else "(no log file)") + "\n```")
    parts.append("</details>")

    return redact("\n".join(parts))


def build_dev_report(
    settings: Settings,
    *,
    buffer: DiagnosticsBuffer | None = None,
    events: int = 60,
    states: int = 40,
) -> str:
    """Render a concise, redacted report for local debugging (e.g. pasting to an assistant).

    Unlike :func:`build_report` — the full report behind the GitHub-issue flow — this
    is scoped to the **current chat session** (everything since the last "New chat",
    via :meth:`DiagnosticsBuffer.mark_session`) and omits the raw log-file tail, so the
    output stays small and focused on the scenario just run. The full log remains on
    disk and the full report still shows everything.

    Args:
        settings: Effective settings (only whitelisted fields are surfaced).
        buffer: Breadcrumb buffer to read; defaults to the process-wide one.
        events: Max recent event lines to include.
        states: Max state-transition lines to include.

    Returns:
        A redacted Markdown report.
    """
    buf = buffer or get_buffer()
    now = datetime.now()
    # Session-scoped: only what happened since the last "New chat".
    session_recent = buf.session_recent()
    counts: Counter[str] = Counter(c.level for c in session_recent)
    count_str = " · ".join(f"{lvl}={n}" for lvl, n in sorted(counts.items())) or "none"

    parts: list[str] = []
    parts.append("# Jack debug (concise)")
    parts.append(
        f"_generated {now:%Y-%m-%d %H:%M:%S}_ · v{_app_version()} · "
        f"{platform.system()} {platform.release()} · Python {platform.python_version()}\n"
    )

    parts.append("## Config")
    parts.append("```\n" + "\n".join(_config_lines(settings)) + "\n```\n")

    parts.append("## Summary")
    parts.append(f"this session: events {count_str} · since {buf.session_started:%H:%M:%S}\n")

    state_trace = buf.session_states()[-states:]
    parts.append("## State sequence")
    parts.append("```\n" + ("\n".join(state_trace) if state_trace else "(none)") + "\n```\n")

    parts.append("## Errors & warnings")
    errs = [c.render() for c in buf.session_errors()]
    parts.append("```\n" + ("\n".join(errs) if errs else "(none)") + "\n```\n")

    recent = [c.render() for c in session_recent][-events:]
    parts.append(f"## Events (last {len(recent)})")
    parts.append("```\n" + ("\n".join(recent) if recent else "(none)") + "\n```\n")

    parts.append("_Full log on this machine: ~/.autobot/logs/autobot.log_")
    return redact("\n".join(parts))


def save_report(
    settings: Settings,
    *,
    buffer: DiagnosticsBuffer | None = None,
    log_path: Path | None = None,
) -> Path:
    """Write the report to ``~/.autobot/reports/debug-report.md`` and return its path.

    Always the same filename (overwritten) so it doesn't pile up — the user opens
    it in Finder to copy or share. The directory derives from ``log_dir``'s parent.
    """
    text = build_report(settings, buffer=buffer, log_path=log_path)
    base = Path(settings.log_dir).expanduser().parent / "reports"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "debug-report.md"
    path.write_text(text, encoding="utf-8")
    return path
