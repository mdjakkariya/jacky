"""Budget command output for the model's context without losing it.

The human sees the full live stream; the model must not. Large output is expensive — it
is re-sent to the model on every subsequent turn — so when output exceeds a cap we write
the full text to a per-run file under ``.jack/command-output/`` (inside the cwd jail,
gitignored) and return only a tail-biased excerpt plus the file path, guiding the model to
``read_file``/``grep`` it for the elided middle. Output at or under the cap is returned
verbatim. Never raises: if the log can't be written, the excerpt is still returned so the
model's context stays bounded.
"""

from __future__ import annotations

import uuid
from pathlib import Path

_MAX_DISK_BYTES = 2_000_000  # never spill more than ~2 MB to a single log file


def _head_tail(full: str, cap: int) -> str:
    """Return the first ~35% and last ~65% of ``cap`` chars, sliced at line boundaries.

    Tail-biased because a command's result/summary/error usually lands at the end.
    """
    head_budget = cap * 35 // 100
    tail_budget = cap - head_budget
    head = full[:head_budget].rsplit("\n", 1)[0]
    tail = full[-tail_budget:].split("\n", 1)[-1]
    elided = len(full) - len(head) - len(tail)
    return f"{head}\n\n[… {elided} chars elided …]\n\n{tail}"


def budget_output(full: str, *, cwd: Path, cap: int) -> str:
    """Return ``full`` inline if within ``cap``; else spill to disk and return an excerpt.

    Args:
        full: The command's complete combined output.
        cwd: The workspace directory (the log is written under ``cwd/.jack``).
        cap: Maximum characters to hand the model inline.

    Returns:
        Either ``full`` unchanged, or a tail-biased excerpt followed by a note giving the
        cwd-relative path of the saved full output.
    """
    if len(full) <= cap:
        return full
    note = "\n\n[full output could not be saved to disk]"
    try:
        out_dir = cwd / ".jack" / "command-output"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{uuid.uuid4().hex[:12]}.log"
        path.write_text(full[:_MAX_DISK_BYTES])
        rel = path.relative_to(cwd).as_posix()
        note = (
            f"\n\nFull output ({full.count(chr(10)) + 1} lines, {len(full)} chars) saved to "
            f"{rel} — use read_file or grep on it for the elided middle."
        )
    except OSError:
        pass  # keep the excerpt-only note; protecting the model's context is what matters
    return _head_tail(full, cap) + note
