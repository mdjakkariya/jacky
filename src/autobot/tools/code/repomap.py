"""Repo map: a compact, bounded overview of a codebase's symbol signatures.

Surfaces the classes/functions defined across a project (their signature lines,
grouped by file) so the model can orient without reading whole files. The signature
extraction uses tree-sitter (Python for now) and is imported lazily, so importing this
module — and running the test suite — stays fast and needs no parser installed. The
value objects and the renderer here are pure: they decide how the map reads and how it
stays within a character budget, and are unit-tested with plain data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


Extractor = Callable[[bytes], list["Symbol"]]

_DEF_NODES = frozenset({"function_definition", "class_definition"})


def extract_python(source: bytes) -> list[Symbol]:  # pragma: no cover - needs the optional parser
    """Extract top-level functions/classes and one level of methods from Python ``source``.

    Uses tree-sitter (imported lazily). Returns signature lines with a ``depth`` so methods
    render indented under their class. Never raises on a parse quirk — a missing name node
    yields ``"?"`` and malformed regions are simply skipped.

    Note:
        The installed ``tree_sitter_language_pack``/``tree-sitter`` build parses ``str``
        source (not ``bytes``) and exposes nodes via methods (``kind()``, ``named_child()``,
        ``start_position()``) rather than the classic properties, so this decodes once
        up front and slices node text by byte offset into the UTF-8 encoding.
    """
    from tree_sitter_language_pack import get_parser

    text = source.decode("utf-8", "replace")
    encoded = text.encode("utf-8")
    parser = get_parser("python")
    tree = parser.parse(text)
    if tree is None:  # defensive: the stub allows it, though real input won't hit this
        return []
    lines = text.split("\n")
    out: list[Symbol] = []

    def node_text(node: Any) -> str:
        start: int = node.start_byte()
        end: int = node.end_byte()
        return encoded[start:end].decode("utf-8", "replace")

    def first_line(node: Any) -> str:
        row: int = node.start_position().row
        return lines[row].rstrip() if row < len(lines) else ""

    def visit(node: Any, depth: int) -> None:
        for i in range(node.named_child_count()):
            child = node.named_child(i)
            if child is None:
                continue
            if child.kind() in _DEF_NODES:
                name_node = child.child_by_field_name("name")
                name = node_text(name_node) if name_node is not None else "?"
                kind = "class" if child.kind() == "class_definition" else "def"
                out.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        line=child.start_position().row + 1,
                        signature=first_line(child),
                        depth=depth,
                    )
                )
                if child.kind() == "class_definition":  # one level down for methods
                    body = child.child_by_field_name("body")
                    if body is not None:
                        visit(body, depth + 1)

    visit(tree.root_node(), 0)
    return out
