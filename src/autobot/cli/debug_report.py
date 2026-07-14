"""Assemble a shareable "debug this coder session" bundle.

When a coder turn gets stuck or a task fails, the user needs an easy way to hand the details
to another assistant. The useful artifacts are the **session transcript** (the conversation +
tool calls, incl. which command timed out) and the **coder-relevant recent log** — not the
voice-assistant config the generic report surfaces. This module reads both on-device, keeps
only what matters (a bounded transcript excerpt + log lines tagged with coder components,
plus any warnings/errors), redacts secrets/paths, and writes a single file the user can copy
(``cat … | pbcopy``) or attach. Pure + never raises — a debug helper must not itself fail.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from autobot.diagnostics import redact

_BUNDLE_NAME = "debug-report.md"
# Log written up to this long after a session's last transcript write still counts as "this
# session" (the final "turn done" line lands just after the last message is persisted).
_SESSION_LOG_GRACE = timedelta(minutes=2)

# Log components worth keeping for a *coder* bug — drops voice noise (listening/tts/toggles/…).
_CODER_COMPONENTS = frozenset(
    {"coder", "llm", "gate", "usage", "cli", "app", "access", "audit", "tools"}
)
_WARN_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})
_LEVEL_RE = re.compile(r"^\S+ \S+ +(\w+) +\[([a-z_/.]+)\]")


def newest_transcript(cwd: str) -> Path | None:
    """The most recent session transcript under ``<cwd>/.jack/sessions``, or None."""
    folder = Path(cwd) / ".jack" / "sessions"
    try:
        files = [p for p in folder.glob("*.jsonl") if p.is_file()]
    except OSError:
        return None
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def transcript_model(path: Path | None) -> str | None:
    """The model recorded in a transcript's ``meta`` row, or None (never raises)."""
    if path is None or not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            row = json.loads(line)
            if isinstance(row, dict) and row.get("type") == "meta":
                model = row.get("model")
                return str(model) if model else None
    except (OSError, ValueError):
        return None
    return None


def _clip(text: str, limit: int) -> str:
    """One-line, length-capped rendering of a value."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _render_msg(role: str, content: Any) -> list[str]:
    """Render one transcript message (string or content-block list) to compact lines."""
    who = "you" if role == "user" else "jack"
    if isinstance(content, str):
        return [f"{who}: {_clip(content, 300)}"] if content.strip() else []
    lines: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and str(block.get("text", "")).strip():
            lines.append(f"{who}: {_clip(block.get('text', ''), 300)}")
        elif kind == "tool_use":
            args = _clip(json.dumps(block.get("input", {})), 120)
            lines.append(f"  → {block.get('name', '?')}({args})")
        elif kind == "tool_result":
            rc = block.get("content")
            if isinstance(rc, list):
                rc = " ".join(str(x.get("text", "")) for x in rc if isinstance(x, dict))
            lines.append(f"    result: {_clip(str(rc), 200)}")
    return lines


def transcript_excerpt(path: Path | None, *, max_msgs: int = 24) -> str:
    """A compact excerpt of the last ``max_msgs`` transcript messages (never raises)."""
    if path is None or not path.exists():
        return "(no transcript found)"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(couldn't read transcript)"
    msgs: list[dict[str, Any]] = []
    for line in raw:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "msg":
            continue
        message = row.get("message")
        if isinstance(message, dict):
            msgs.append(message)
    out: list[str] = []
    for message in msgs[-max_msgs:]:
        out.extend(_render_msg(str(message.get("role", "?")), message.get("content")))
    return "\n".join(out) or "(empty transcript)"


def _line_ts(line: str) -> datetime | None:
    """Parse the leading ``YYYY-mm-dd HH:MM:SS`` timestamp of a log line, or None."""
    try:
        return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def coder_log_tail(log_path: Path, *, n: int = 60, before: datetime | None = None) -> str:
    """The last ``n`` coder-relevant log lines (+ any warnings/errors), redacted.

    ``before`` scopes to one session: lines timestamped after it are dropped — so a later run
    (e.g. a test suite, or a subsequent session) can't bury the session being debugged. Lines
    carrying a pytest tmp path are dropped outright as residual test noise.
    """
    try:
        raw = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log file)"
    cutoff = before + _SESSION_LOG_GRACE if before is not None else None
    kept: list[str] = []
    for line in raw:
        if "pytest-of" in line or "/pytest-" in line:
            continue  # residual test noise from a previously-polluted log
        match = _LEVEL_RE.match(line)
        if match is None:
            continue  # a traceback/continuation line — skip (header line is kept instead)
        level, component = match.group(1), match.group(2)
        if component not in _CODER_COMPONENTS and level not in _WARN_LEVELS:
            continue
        if cutoff is not None:
            ts = _line_ts(line)
            if ts is not None and ts > cutoff:
                continue  # after this session — not part of what we're debugging
        kept.append(line)
    return redact("\n".join(kept[-n:])) if kept else "(no coder log lines for this session)"


def context_line(
    usage: dict[str, Any], autonomy: str, *, model: str | None = None, provider: str | None = None
) -> str:
    """A one-line model/provider/autonomy + session token/cost summary.

    ``model``/``provider`` are fallbacks (from the transcript meta / settings) used when live
    usage isn't available — e.g. ``jack debug`` run standalone with no daemon to query.
    """
    session = usage.get("session") if isinstance(usage, dict) else None
    model = (usage.get("model") if isinstance(usage, dict) else None) or model
    provider = (usage.get("provider") if isinstance(usage, dict) else None) or provider
    base = f"{model or '?'} ({provider or '?'}) · autonomy {autonomy}"
    if not isinstance(session, dict) or not session.get("turns"):
        return f"{base} · no usage recorded yet"
    usd = session.get("usd")
    cost = f"${float(usd):.4f}" if isinstance(usd, (int, float)) else "n/a"
    cr = int(session.get("cache_read", 0))
    cw = int(session.get("cache_write", 0))
    return (
        f"{base} · {session.get('turns', 0)} turns · "
        f"in {int(session.get('in', 0)):,} / out {int(session.get('out', 0)):,} · "
        f"cache r {cr:,} / w {cw:,} · {cost}"
    )


def build_bundle(
    *,
    transcript: Path | None,
    log_path: Path,
    cwd: str,
    usage: dict[str, Any],
    autonomy: str,
    provider: str | None = None,
) -> str:
    """Assemble the coder debug bundle: context + transcript excerpt + coder log tail.

    The log is scoped to the session's window (up to the transcript's last-write time) so a
    later run can't bury it; model falls back to the transcript's meta and provider to
    ``provider`` (from settings) when live usage isn't available.
    """
    transcript_str = str(transcript) if transcript is not None else "(none found)"
    before: datetime | None = None
    if transcript is not None:
        try:
            before = datetime.fromtimestamp(transcript.stat().st_mtime)
        except OSError:
            before = None
    context = context_line(usage, autonomy, model=transcript_model(transcript), provider=provider)
    parts = [
        "# Jack — session debug bundle",
        "",
        "Paste this to your coding assistant (or a bug report) to debug a stuck or failed "
        "Jack coder session. It has the recent conversation + tool calls and the coder-"
        "relevant log lines for this session (secrets/paths redacted).",
        "",
        f"- Workspace: {cwd}",
        f"- Session: {context}",
        f"- Transcript: {transcript_str}",
        "- Full log on this machine: ~/.autobot/logs/autobot.log",
        "",
        "## Transcript (recent steps)",
        "",
        "```",
        transcript_excerpt(transcript),
        "```",
        "",
        "## Recent coder log",
        "",
        "```",
        coder_log_tail(log_path, before=before),
        "```",
        "",
    ]
    return "\n".join(parts)


def write_bundle(text: str, cwd: str) -> Path:
    """Write the bundle to ``<cwd>/.jack/debug-report.md`` (overwritten); return its path."""
    path = Path(cwd) / ".jack" / _BUNDLE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def share_hint(path: Path, transcript: Path | None) -> str:
    """The copy/paste instructions printed after writing a bundle."""
    lines = [
        f"Wrote a shareable debug report → {path}",
        f"  Copy it to share:  cat '{path}' | pbcopy   (then paste to your assistant)",
    ]
    if transcript is not None:
        lines.append(f"  Or attach the report + the transcript: {transcript}")
    return "\n".join(lines)
