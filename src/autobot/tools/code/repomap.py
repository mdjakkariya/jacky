"""Repo map: a compact, bounded overview of a codebase's symbol signatures.

Surfaces the classes/functions defined across a project (their signature lines,
grouped by file) so the model can orient without reading whole files. The signature
extraction uses tree-sitter (Python for now) and is imported lazily, so importing this
module — and running the test suite — stays fast and needs no parser installed. The
value objects and the renderer here are pure: they decide how the map reads and how it
stays within a character budget, and are unit-tested with plain data.
"""

from __future__ import annotations

from dataclasses import dataclass

_DEFAULT_CHAR_BUDGET = 8000


@dataclass(frozen=True, slots=True)
class Symbol:
    """One defined symbol: name, kind (``def``/``class``), 1-based line, and signature line."""

    name: str
    kind: str
    line: int
    signature: str
    depth: int  # nesting level (0 = top-level); indents methods under their class


@dataclass(frozen=True, slots=True)
class FileMap:
    """The symbols defined in one file (path relative to the scanned root)."""

    path: str
    symbols: tuple[Symbol, ...]


def render_repo_map(file_maps: list[FileMap], char_budget: int = _DEFAULT_CHAR_BUDGET) -> str:
    """Render ``file_maps`` as a compact, path-ordered, budget-bounded signature overview."""
    with_syms = sorted((fm for fm in file_maps if fm.symbols), key=lambda fm: fm.path)
    if not with_syms:
        return "No symbols found — the repo map is empty."
    blocks: list[str] = []
    used = 0
    dropped = 0
    for fm in with_syms:
        lines = [fm.path]
        lines += [f"  {'  ' * s.depth}{s.signature.strip()}" for s in fm.symbols]
        block = "\n".join(lines)
        if used + len(block) + 1 > char_budget and blocks:
            dropped = len(with_syms) - with_syms.index(fm)
            break
        blocks.append(block)
        used += len(block) + 1
    body = "\n".join(blocks)
    if dropped:
        body += f"\n…({dropped} more file(s) not shown; raise the budget or narrow the path)"
    return body
