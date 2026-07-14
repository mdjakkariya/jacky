"""Assemble a shareable "debug this session" bundle for the coder CLI.

When a turn gets stuck or a task fails, the user needs an easy way to hand the details to
another assistant (or a bug report). The daemon already builds a redacted, bounded report
of recent log events/errors (``autobot.diagnostics``); this module wraps it with a pointer
to the session transcript and the session's token/cost summary, and writes it to a single
file the user can copy (``cat … | pbcopy``) or attach. Pure + never raises — a debug helper
must not itself fail when things are already going wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_BUNDLE_NAME = "debug-report.md"


def newest_transcript(cwd: str) -> Path | None:
    """The most recent session transcript under ``<cwd>/.jack/sessions``, or None.

    The coder writes one ``<id>.jsonl`` per session there; the newest by mtime is the
    session the user is (or just was) in — the one worth sharing to debug.
    """
    folder = Path(cwd) / ".jack" / "sessions"
    try:
        files = [p for p in folder.glob("*.jsonl") if p.is_file()]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def cost_line(usage: dict[str, Any]) -> str:
    """A one-line session token/cost summary from a ``/coder/usage`` payload."""
    session = usage.get("session") if isinstance(usage, dict) else None
    if not isinstance(session, dict) or not session.get("turns"):
        return "Session usage: (none recorded yet)"
    model = usage.get("model") or "?"
    provider = usage.get("provider") or "?"
    usd = session.get("usd")
    cost = f"${float(usd):.4f}" if isinstance(usd, (int, float)) else "n/a"
    cr = int(session.get("cache_read", 0))
    cw = int(session.get("cache_write", 0))
    return (
        f"Session usage: {session.get('turns', 0)} turns · "
        f"in {int(session.get('in', 0)):,} / out {int(session.get('out', 0)):,} · "
        f"cache r {cr:,} / w {cw:,} · {cost} ({model}, {provider})"
    )


def build_bundle(report_md: str, *, transcript: Path | None, cost: str) -> str:
    """Wrap the daemon report with a how-to-share header, the transcript path, and cost."""
    transcript_str = str(transcript) if transcript is not None else "(none found)"
    header = [
        "# Jack — session debug bundle",
        "",
        "Paste this to your coding assistant (or a bug report) to debug a stuck or failed "
        "Jack session. It contains the config, recent log events + errors, and pointers to "
        "the full transcript and log.",
        "",
        f"- Transcript (full conversation + tool calls): {transcript_str}",
        f"- {cost}",
        "- Full log on this machine: ~/.autobot/logs/autobot.log",
        "",
        "---",
        "",
    ]
    body = report_md.strip() or "(daemon report unavailable — share the transcript + log above)"
    return "\n".join(header) + body + "\n"


def write_bundle(text: str, cwd: str) -> Path:
    """Write the bundle to ``<cwd>/.jack/debug-report.md`` (overwritten); return its path.

    One fixed filename so reports don't pile up; ``.jack/`` is gitignored.
    """
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
