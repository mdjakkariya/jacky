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
from pathlib import Path
from typing import Any

from autobot.diagnostics import redact

_BUNDLE_NAME = "debug-report.md"

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


def coder_log_tail(log_path: Path, *, n: int = 60) -> str:
    """The last ``n`` coder-relevant log lines (+ any warnings/errors), redacted."""
    try:
        raw = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log file)"
    kept: list[str] = []
    for line in raw:
        match = _LEVEL_RE.match(line)
        if match is None:
            continue  # a traceback/continuation line — skip (header line is kept instead)
        level, component = match.group(1), match.group(2)
        if component in _CODER_COMPONENTS or level in _WARN_LEVELS:
            kept.append(line)
    return redact("\n".join(kept[-n:])) if kept else "(no coder log lines)"


def context_line(usage: dict[str, Any], autonomy: str) -> str:
    """A one-line model/provider/autonomy + session token/cost summary."""
    session = usage.get("session") if isinstance(usage, dict) else None
    model = usage.get("model") if isinstance(usage, dict) else None
    provider = usage.get("provider") if isinstance(usage, dict) else None
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
) -> str:
    """Assemble the coder debug bundle: context + transcript excerpt + coder log tail."""
    transcript_str = str(transcript) if transcript is not None else "(none found)"
    parts = [
        "# Jack — session debug bundle",
        "",
        "Paste this to your coding assistant (or a bug report) to debug a stuck or failed "
        "Jack coder session. It has the recent conversation + tool calls and the coder-"
        "relevant log lines (secrets/paths redacted).",
        "",
        f"- Workspace: {cwd}",
        f"- Session: {context_line(usage, autonomy)}",
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
        coder_log_tail(log_path),
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
