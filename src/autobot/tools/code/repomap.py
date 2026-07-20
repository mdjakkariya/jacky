"""Repo map: a compact, bounded overview of a codebase's symbol signatures.

Surfaces the classes/functions defined across a project (their signature lines,
grouped by file) so the model can orient without reading whole files. The signature
extraction uses tree-sitter (Python, JavaScript/TypeScript, Go, Rust — all bundled by
``tree_sitter_language_pack``) and is imported lazily, so importing this module — and
running the test suite — stays fast and needs no parser installed. The value objects and
the renderer here are pure: they decide how the map reads and how it stays within a
character budget, and are unit-tested with plain data.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.registry import ToolRegistry, ToolSpec

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


@dataclass(frozen=True, slots=True)
class _LangSpec:
    """Per-language tree-sitter node kinds for symbol extraction.

    ``def_nodes`` are the node kinds emitted as a signature line; ``class_nodes`` is the
    subset that has members worth listing (a class/impl/interface body), which the walker
    descends into one level so methods render indented under their container. ``wrapper_nodes``
    are transparent wrappers the walker descends *at the same depth* to reach the definition
    inside — e.g. an ``export_statement`` (JS/TS) or a ``decorated_definition`` (Python), so an
    exported or decorated def isn't missed.
    """

    language: str  # tree-sitter language name passed to get_parser
    def_nodes: frozenset[str]
    class_nodes: frozenset[str]
    wrapper_nodes: frozenset[str] = frozenset()


# The languages tree_sitter_language_pack bundles that we map — no extra dependency per
# language. Node/field names are the tree-sitter grammar's; a wrong guess just misses a few
# symbols (never raises — build_repo_map skips a file that fails to parse).
_PY = _LangSpec(
    "python",
    frozenset({"function_definition", "class_definition"}),
    frozenset({"class_definition"}),
    frozenset({"decorated_definition"}),  # @decorator wraps the def/class
)
_JS = _LangSpec(
    "javascript",
    frozenset(
        {
            "function_declaration",
            "generator_function_declaration",
            "class_declaration",
            "method_definition",
        }
    ),
    frozenset({"class_declaration"}),
    frozenset({"export_statement"}),  # `export function/class …`
)
_TS_DEFS = frozenset(
    {
        "function_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "method_definition",
        "interface_declaration",
        "enum_declaration",
    }
)
_TS_CLASSES = frozenset(
    {"class_declaration", "abstract_class_declaration", "interface_declaration"}
)
_TS_WRAP = frozenset({"export_statement"})
_GO = _LangSpec(
    "go", frozenset({"function_declaration", "method_declaration", "type_declaration"}), frozenset()
)
_RUST = _LangSpec(
    "rust",
    frozenset({"function_item", "struct_item", "enum_item", "trait_item", "impl_item", "mod_item"}),
    frozenset({"impl_item", "trait_item"}),
)

_SPECS: dict[str, _LangSpec] = {
    "python": _PY,
    "javascript": _JS,
    "typescript": _LangSpec("typescript", _TS_DEFS, _TS_CLASSES, _TS_WRAP),
    "tsx": _LangSpec("tsx", _TS_DEFS, _TS_CLASSES, _TS_WRAP),
    "go": _GO,
    "rust": _RUST,
}

# File extension -> language key. Unlisted extensions are not scanned.
_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
}


def _extract(source: bytes, spec: _LangSpec) -> list[Symbol]:  # pragma: no cover - needs the parser
    """Extract top-level defs/classes and one level of members from ``source`` per ``spec``.

    Uses tree-sitter (imported lazily). Returns signature lines with a ``depth`` so members
    render indented under their container. Never raises on a parse quirk — a missing name node
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
    parser = get_parser(spec.language)
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
            kind = child.kind()
            if kind in spec.wrapper_nodes:  # transparent wrapper (export/decorator) — see through
                visit(child, depth)
            elif kind in spec.def_nodes:
                name_node = child.child_by_field_name("name")
                name = node_text(name_node) if name_node is not None else "?"
                out.append(
                    Symbol(
                        name=name,
                        kind="class" if kind in spec.class_nodes else "def",
                        line=child.start_position().row + 1,
                        signature=first_line(child),
                        depth=depth,
                    )
                )
                if kind in spec.class_nodes:  # one level down for members (methods etc.)
                    body = child.child_by_field_name("body")
                    if body is not None:
                        visit(body, depth + 1)

    visit(tree.root_node(), 0)
    return out


def extract_python(source: bytes) -> list[Symbol]:  # pragma: no cover - needs the optional parser
    """Extract Python symbols (kept for callers/tests; delegates to the generic extractor)."""
    return _extract(source, _PY)


def _extractor_for(language: str) -> Extractor:
    """Return a bytes->symbols extractor bound to ``language``'s tree-sitter spec."""
    spec = _SPECS[language]

    def extract(source: bytes) -> list[Symbol]:
        return _extract(source, spec)

    return extract


_log = get_logger("coder")

_MAX_FILES = 400  # cap files scanned per build
_MAX_FILE_BYTES = 500_000  # skip files larger than this
_SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".ruff_cache", ".tox"}
)

# module-level cache: abs path -> (mtime, size, symbols). The daemon is long-lived, so an
# in-memory cache survives across turns; entries self-heal when a file's mtime/size changes.
_CACHE: dict[str, tuple[float, int, tuple[Symbol, ...]]] = {}


def build_repo_map(
    root: str,
    broker: AccessBroker,
    *,
    char_budget: int = _DEFAULT_CHAR_BUDGET,
    extractor: Extractor | None = None,
) -> str:
    """Scan the jailed ``root`` for source files and render a bounded symbol overview.

    Resolves ``root`` through ``broker`` (read-only, prompting for a grant if needed),
    walks the tree pruning noise directories and skipping oversized files, and extracts
    each file's symbols with the extractor for its language (Python/JS/TS/Go/Rust), or an
    injected ``extractor`` override when provided (tests), consulting a
    module-level mtime+size cache so unchanged files are not re-parsed. Never raises: a
    denied path, a non-folder target, an empty tree, or a single unreadable/unparseable
    file all degrade to a friendly string or a skipped file rather than an exception.
    """
    try:
        base = broker.ensure(root or ".", write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not base.is_dir():
        return f"'{base.name}' is not a folder to map."

    file_maps: list[FileMap] = []
    scanned = 0
    for dirpath, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            language = _LANG_BY_EXT.get(Path(name).suffix)
            if language is None:  # not a source file we know how to map
                continue
            if scanned >= _MAX_FILES:
                break
            fp = Path(dirpath) / name
            try:
                st = fp.stat()
                if st.st_size > _MAX_FILE_BYTES:
                    continue
                cached = _CACHE.get(str(fp))
                if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
                    symbols = cached[2]
                else:
                    # An injected extractor (tests) overrides language dispatch; otherwise pick
                    # the per-language extractor by the file's extension.
                    extract = extractor or _extractor_for(language)
                    symbols = tuple(extract(fp.read_bytes()))
                    _CACHE[str(fp)] = (st.st_mtime, st.st_size, symbols)
            except Exception:  # skip a file we can't stat, read, or parse
                continue
            scanned += 1
            rel = str(fp.relative_to(base))
            file_maps.append(FileMap(path=rel, symbols=symbols))
    if not file_maps:
        return "No supported source files found under this path."
    _log.info("repo_map root=%s files=%d", base.name, len(file_maps))
    return render_repo_map(file_maps, char_budget)


def register_repomap_tool(registry: ToolRegistry, broker: AccessBroker) -> None:
    """Register the read-only ``repo_map`` tool (needs the optional ``code`` extra to run)."""

    def _handler(path: str = ".") -> str:
        return build_repo_map(path, broker)

    registry.register(
        ToolSpec(
            name="repo_map",
            description=(
                "Show a compact overview of the code in a folder — the classes and functions "
                "defined in each file, with their signature lines — so you can orient without "
                "reading whole files. Pass `path` to map a subfolder (defaults to the working "
                "folder). Use grep/read_file to then dig into specifics."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Folder to map (optional)."},
                },
                "required": [],
            },
            handler=_handler,
            risk=Risk.READ_ONLY,
            ack="Mapping the code.",
        )
    )
    _log.info("repo_map tool registered")
