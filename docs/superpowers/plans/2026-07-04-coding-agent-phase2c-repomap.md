# Coding-agent Phase 2c — Repo map (#50) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `repo_map` capability to the coder tools: surface a compact, bounded overview of a codebase's file/symbol signatures (Python for v1) without loading whole files, so the model can orient in a repo cheaply. Built on tree-sitter (lazy, optional), path-jailed, and exposed as a `repo_map` tool.

**Architecture:** A **pure core** in `src/autobot/tools/code/repomap.py` — value objects (`Symbol`, `FileMap`) plus a pure `render_repo_map(file_maps, char_budget) -> str` that groups signatures by file, orders by path, and truncates to a budget. A thin **extraction layer** `extract_python(source: bytes) -> list[Symbol]` parses one file with tree-sitter (lazily imported via `tree_sitter_language_pack`, so importing the module stays fast and dependency-free). An **orchestrator** `build_repo_map(root, broker, *, char_budget, extractor) -> str` walks the jailed tree, extracts each Python file's symbols (caching on mtime), and renders — with the extractor **injected** so unit tests exercise the walk/cache/render with fake symbols and never need tree-sitter. Registration adds a `repo_map` (READ_ONLY) tool via the existing `register_code_tools`.

**Tech Stack:** Python ≥ 3.11. New optional extra `code = ["tree-sitter>=0.23", "tree-sitter-language-pack>=0.9"]`, lazy-imported (CI installs only `dev`/`daemon`, so tree-sitter is absent there — the pure core is fully tested without it; the extraction path is `# pragma: no cover` + an `importorskip` integration test). Existing `AccessBroker`, `ToolRegistry`/`ToolSpec`/`Risk`. Tests: `pytest`, explicit fakes.

## Global Constraints

Every task's requirements implicitly include this section (copied verbatim from `CLAUDE.md`/epic HARD RULES).

- **Conventional Commits**; **NO Co-Authored-By / no AI-attribution trailer**. **No reference to any external tool/product** in committed code/docs (describe our own behavior; naming *Aider*, *claude-code*, etc. is NOT allowed — even the word "à la Aider" from the spec must not appear in code).
- **Stage EXPLICIT paths only** — never `git add -A`/`.`/`-u`.
- **`make check` green** (ruff + ruff-format + mypy strict + pytest) before a task is DONE. `warn_unused_ignores=true`. Don't hand-format; run `make format`.
- `from __future__ import annotations` in every module; full type hints; value objects `@dataclass(frozen=True, slots=True)`; **line length 100**; Google-style docstrings (tests exempt).
- **Tools return strings and never raise out of the handler**; path jail via `broker.ensure(path, write=False)` (repo map is read-only).
- **Heavy runtime imported lazily** — `tree_sitter_language_pack` is imported *inside* `extract_python`, never at module top, so importing `repomap.py` (and the test suite) stays fast and works without the extra installed.
- **Logging:** `_log = get_logger("coder")`; seam events at INFO (files scanned, symbols, cache hits), not per-node noise.
- **English only.** Tests: `uv run pytest <path> -q`.

## Deliberately deferred (do NOT build in #50; interfaces stay stable)
- **Symbol ranking** (reference-count / PageRank importance ordering). v1 orders files by path and lists all signatures within the budget. When ranking lands it slots into `render_repo_map`/orchestration without changing the tool.
- **Multi-language** extraction (only Python in v1; the extractor is dispatched by language so more can be added as data).
- **Persistent / cross-run cache** (v1 caches in-memory on the `RepoMap` instance, keyed on mtime).
- **Context injection** into the coder profile at turn start (that's #53; v1 exposes `repo_map` as an on-demand tool).

---

### Task 1: Value objects + pure renderer (`repomap.py`)

Pure, no I/O, no tree-sitter. The part that decides *how the map looks* and stays under budget.

**Files:**
- Create: `src/autobot/tools/code/repomap.py`
- Test: `tests/unit/test_code_repomap.py`

**Interfaces:**
- Produces (Tasks 2–4 rely on these):
  - `Symbol(name: str, kind: str, line: int, signature: str, depth: int)` — frozen/slots.
  - `FileMap(path: str, symbols: tuple[Symbol, ...])` — frozen/slots.
  - `render_repo_map(file_maps: list[FileMap], char_budget: int = 8000) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_code_repomap.py`:

```python
"""Tests for the repo-map pure core (value objects + renderer). No tree-sitter needed."""

from __future__ import annotations

from autobot.tools.code.repomap import FileMap, Symbol, render_repo_map


def _fm(path: str, *syms: tuple[str, str, int, str, int]) -> FileMap:
    return FileMap(path=path, symbols=tuple(Symbol(*s) for s in syms))


def test_render_groups_by_file_and_shows_signatures() -> None:
    fm = _fm(
        "pkg/a.py",
        ("Greeter", "class", 1, "class Greeter:", 0),
        ("hello", "def", 2, "    def hello(self, name):", 1),
    )
    out = render_repo_map([fm])
    assert "pkg/a.py" in out
    assert "class Greeter:" in out
    assert "def hello(self, name):" in out
    # the class line appears before its method
    assert out.index("class Greeter:") < out.index("def hello")


def test_render_orders_files_by_path() -> None:
    fm_b = _fm("b.py", ("b", "def", 1, "def b():", 0))
    fm_a = _fm("a.py", ("a", "def", 1, "def a():", 0))
    out = render_repo_map([fm_b, fm_a])
    assert out.index("a.py") < out.index("b.py")


def test_render_empty_is_friendly() -> None:
    assert "no" in render_repo_map([]).lower()


def test_render_skips_files_with_no_symbols() -> None:
    out = render_repo_map([_fm("empty.py"), _fm("x.py", ("f", "def", 1, "def f():", 0))])
    assert "empty.py" not in out
    assert "x.py" in out


def test_render_respects_char_budget() -> None:
    files = [_fm(f"f{i}.py", ("g", "def", 1, "def g():", 0)) for i in range(200)]
    out = render_repo_map(files, char_budget=300)
    assert len(out) <= 400  # budget + a short truncation note
    assert "more" in out.lower() or "truncat" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail** — `uv run pytest tests/unit/test_code_repomap.py -q` → FAIL (ImportError).

- [ ] **Step 3: Implement the pure core**

Create `src/autobot/tools/code/repomap.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/unit/test_code_repomap.py -q` → PASS (5).
- [ ] **Step 5: `make check`** → green.
- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/code/repomap.py tests/unit/test_code_repomap.py
git commit -m "feat(code): repo-map value objects + pure budget-bounded renderer (#50)"
```

---

### Task 2: Tree-sitter Python extraction (`repomap.py`)

Parse one Python file into `Symbol`s with tree-sitter. Lazy import; excluded from coverage (optional-dependency boundary) and validated by an `importorskip` integration test that really parses.

**Files:**
- Modify: `src/autobot/tools/code/repomap.py`
- Test: `tests/unit/test_code_repomap.py` (append)

**Interfaces:**
- Produces: `extract_python(source: bytes) -> list[Symbol]`; `Extractor = Callable[[bytes], list[Symbol]]` (Task 3 injects this).

- [ ] **Step 1: Write the failing integration test (append to the test file)**

```python
# add near the top imports:
import pytest

from autobot.tools.code.repomap import extract_python


def test_extract_python_finds_classes_functions_methods() -> None:
    ts = pytest.importorskip("tree_sitter_language_pack")  # needs the optional `code` extra
    assert ts  # importorskip returns the module
    src = b"import os\n\n\ndef top():\n    return 1\n\n\nclass C:\n    def m(self, x):\n        return x\n"
    syms = extract_python(src)
    names = {(s.name, s.kind, s.depth) for s in syms}
    assert ("top", "def", 0) in names
    assert ("C", "class", 0) in names
    assert ("m", "def", 1) in names  # method nested under the class
    method = next(s for s in syms if s.name == "m")
    assert method.signature.strip().startswith("def m(self, x):")
    assert method.line == 9
```

- [ ] **Step 2: Verify it fails** — `uv run pytest tests/unit/test_code_repomap.py -q` → FAIL (`extract_python` not defined). (If the extra isn't installed it would skip, but the import of `extract_python` at module top makes collection fail first — that's the RED we want.)

- [ ] **Step 3: Implement `extract_python` (append to `repomap.py`)**

Add `from collections.abc import Callable` and `from typing import Any` to the imports, then append:

```python
Extractor = Callable[[bytes], list["Symbol"]]

_DEF_NODES = frozenset({"function_definition", "class_definition"})


def extract_python(source: bytes) -> list[Symbol]:  # pragma: no cover - needs the optional parser
    """Extract top-level functions/classes and one level of methods from Python ``source``.

    Uses tree-sitter (imported lazily). Returns signature lines with a ``depth`` so methods
    render indented under their class. Never raises on a parse quirk — a missing name node
    yields ``"?"`` and malformed regions are simply skipped.
    """
    from tree_sitter_language_pack import get_parser

    parser = get_parser("python")
    tree = parser.parse(source)
    lines = source.split(b"\n")
    out: list[Symbol] = []

    def first_line(node: Any) -> str:
        row = node.start_point[0]
        return lines[row].decode("utf-8", "replace").rstrip() if row < len(lines) else ""

    def visit(node: Any, depth: int) -> None:
        for child in node.children:
            if child.type in _DEF_NODES:
                name_node = child.child_by_field_name("name")
                name = name_node.text.decode("utf-8", "replace") if name_node is not None else "?"
                kind = "class" if child.type == "class_definition" else "def"
                out.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        line=child.start_point[0] + 1,
                        signature=first_line(child),
                        depth=depth,
                    )
                )
                if child.type == "class_definition":  # one level down for methods
                    body = child.child_by_field_name("body")
                    if body is not None:
                        visit(body, depth + 1)

    visit(tree.root_node, 0)
    return out
```

- [ ] **Step 4: Install the extra and verify the integration test really runs (not skips)**

Run: `uv sync --extra code` then `uv run pytest tests/unit/test_code_repomap.py::test_extract_python_finds_classes_functions_methods -v`
Expected: PASS (not SKIPPED). This proves the tree-sitter code is correct. If it errors on the tree-sitter API, fix `extract_python` to match the installed `tree_sitter_language_pack`/`tree-sitter` API (e.g. `node.text`, `child_by_field_name`, `start_point`) — report exactly what you changed.

- [ ] **Step 5: `make check`** → green (the integration test may show as skipped in the coverage run if the extra isn't on the default sync — that's fine; you validated it in Step 4).

- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/code/repomap.py tests/unit/test_code_repomap.py
git commit -m "feat(code): tree-sitter Python symbol extraction for the repo map (#50)"
```

---

### Task 3: Orchestrator `build_repo_map` + mtime cache (`repomap.py`)

Walk the jailed tree for Python files, extract each (caching on mtime), render. The extractor is injected so unit tests use a fake and never touch tree-sitter.

**Files:**
- Modify: `src/autobot/tools/code/repomap.py`
- Test: `tests/unit/test_code_repomap.py` (append)

**Interfaces:**
- Consumes: `render_repo_map`, `extract_python`, `Symbol`/`FileMap`, `AccessBroker`/`AccessDeniedError`.
- Produces (Task 4 relies on this): `build_repo_map(root: str, broker: AccessBroker, *, char_budget: int = _DEFAULT_CHAR_BUDGET, extractor: Extractor | None = None) -> str`; constants `_MAX_FILES = 400`, `_MAX_FILE_BYTES = 500_000`, `_SKIP_DIRS`.

- [ ] **Step 1: Write the failing tests (append)**

```python
from pathlib import Path

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.repomap import Symbol, build_repo_map


class _FakeConfirmer:
    def __init__(self, grant: bool) -> None:
        self._grant = grant

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return self._grant

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default if self._grant else ""


def _broker(tmp_path: Path, *, grant: bool = True) -> AccessBroker:
    pol = AccessPolicy(store_path=tmp_path / "access.json", workspace_root=tmp_path / "ws")
    return AccessBroker(pol, _FakeConfirmer(grant))


def _fake_extractor(source: bytes) -> list[Symbol]:
    # trivial deterministic "parser": one symbol per line beginning with "def "
    out: list[Symbol] = []
    for i, ln in enumerate(source.decode().splitlines(), start=1):
        if ln.startswith("def "):
            out.append(Symbol(ln[4:].split("(")[0], "def", i, ln, 0))
    return out


def test_build_repo_map_scans_python_files(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def alpha():\n    pass\n")
    (tmp_path / "pkg" / "b.py").write_text("def beta():\n    pass\n")
    (tmp_path / "notes.txt").write_text("def not_code():\n")  # non-.py ignored
    out = build_repo_map(str(tmp_path), _broker(tmp_path), extractor=_fake_extractor)
    assert "a.py" in out and "alpha" in out
    assert "b.py" in out and "beta" in out
    assert "notes.txt" not in out


def test_build_repo_map_denied(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def x():\n    pass\n")
    out = build_repo_map(str(tmp_path), _broker(tmp_path, grant=False), extractor=_fake_extractor)
    assert "don't have access" in out.lower()


def test_build_repo_map_empty_tree(tmp_path: Path) -> None:
    out = build_repo_map(str(tmp_path), _broker(tmp_path), extractor=_fake_extractor)
    assert "no" in out.lower()  # "No Python files" / "No symbols"


def test_build_repo_map_uses_cache_on_second_call(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def x():\n    pass\n")
    calls: list[bytes] = []

    def counting(source: bytes) -> list[Symbol]:
        calls.append(source)
        return _fake_extractor(source)

    b = _broker(tmp_path)
    build_repo_map(str(tmp_path), b, extractor=counting)
    build_repo_map(str(tmp_path), b, extractor=counting)  # unchanged file → cached
    assert len(calls) == 1  # extractor invoked once across two builds
```

- [ ] **Step 2: Verify it fails** — `uv run pytest tests/unit/test_code_repomap.py -q` → FAIL (`build_repo_map` not defined).

- [ ] **Step 3: Implement `build_repo_map` + cache (append to `repomap.py`)**

Add `import os` and `from pathlib import Path` to the imports, and `from autobot.logging_setup import get_logger` + `from autobot.tools.access import AccessBroker, AccessDeniedError`, then append:

```python
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
    """Scan the jailed ``root`` for Python files and render a bounded symbol overview."""
    extract = extractor or extract_python
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
            if not name.endswith(".py"):
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
                    symbols = tuple(extract(fp.read_bytes()))
                    _CACHE[str(fp)] = (st.st_mtime, st.st_size, symbols)
            except OSError:
                continue
            scanned += 1
            rel = str(fp.relative_to(base))
            file_maps.append(FileMap(path=rel, symbols=symbols))
    if not file_maps:
        return "No Python files found under this path."
    _log.info("repo_map root=%s files=%d", base.name, len(file_maps))
    return render_repo_map(file_maps, char_budget)
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/unit/test_code_repomap.py -q` → PASS.
- [ ] **Step 5: `make check`** → green.
- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/code/repomap.py tests/unit/test_code_repomap.py
git commit -m "feat(code): repo-map orchestration over a jailed tree with mtime cache (#50)"
```

---

### Task 4: Register `repo_map` tool + `code` extra + mypy override

Expose `repo_map` (READ_ONLY) via the existing `register_code_tools`; add the optional dependency and the mypy override so strict type-checking passes without the parser installed.

**Files:**
- Modify: `src/autobot/tools/code/repomap.py` (add `register_repomap_tool`)
- Modify: `src/autobot/tools/code/tools.py` (call it from `register_code_tools`)
- Modify: `pyproject.toml` (add `code` extra + `tree_sitter*` mypy override)
- Test: `tests/unit/test_code_tools.py` (append)

**Interfaces:**
- Produces: `register_repomap_tool(registry: ToolRegistry, broker: AccessBroker) -> None`.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_code_tools.py`)**

```python
def test_register_adds_repo_map(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.get("repo_map") is not None


def test_repo_map_risk_and_no_arg_safe(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    spec = reg.get("repo_map")
    assert spec is not None
    assert spec.risk == Risk.READ_ONLY
    out = spec.handler()  # no args → must return a string, never raise
    assert isinstance(out, str) and out
```

- [ ] **Step 2: Verify it fails** — `uv run pytest tests/unit/test_code_tools.py -q` → FAIL (`repo_map` not registered).

- [ ] **Step 3: Add `register_repomap_tool` to `repomap.py`**

Add `from autobot.core.types import Risk` and `from autobot.tools.registry import ToolRegistry, ToolSpec` to `repomap.py`'s imports, then append:

```python
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
```

- [ ] **Step 4: Wire it into `register_code_tools` (`tools.py`)**

Add `from autobot.tools.code.repomap import register_repomap_tool` with the existing imports, and add this line at the END of `register_code_tools` (after the `register_exec_tools(registry, broker)` call):

```python
    register_repomap_tool(registry, broker)
```

- [ ] **Step 5: Add the `code` extra + mypy override (`pyproject.toml`)**

In `[project.optional-dependencies]`, add:

```toml
# Repo map (Phase 2 coding agent): tree-sitter symbol extraction. Opt-in, lazy-imported;
# prebuilt wheels for many languages so no compiler is needed: `uv sync --extra code`.
code = [
    "tree-sitter>=0.23",
    "tree-sitter-language-pack>=0.9",
]
```

In the `[[tool.mypy.overrides]]` `module` list (the "Third-party runtimes ship no type stubs" block), add:

```toml
    "tree_sitter_language_pack.*",
    "tree_sitter.*",
```

- [ ] **Step 6: Run tests** — `uv run pytest tests/unit/test_code_tools.py -q` → PASS.
- [ ] **Step 7: `make check`** → green (whole suite; tree-sitter still not required — the extraction path is `# pragma: no cover` and the integration test skips when the extra is absent).
- [ ] **Step 8: Commit**

```bash
git add src/autobot/tools/code/repomap.py src/autobot/tools/code/tools.py pyproject.toml tests/unit/test_code_tools.py
git commit -m "feat(code): register repo_map tool + add optional tree-sitter code extra (#50)"
```

---

## Notes for the executor

- **Scope discipline (YAGNI):** #50 is only the repo map (pure core + Python extraction + orchestration + the `repo_map` tool + the dependency). Do NOT add ranking, other languages, a persistent cache, or profile context-injection (those are deferred / #53).
- **The tree-sitter API is the one risk.** In Task 2 Step 4 you MUST `uv sync --extra code` and confirm the integration test actually PASSES (not skips) — that's the only real check on the parser code. If the installed API differs (method/attribute names), adapt `extract_python` and report the change; keep the `# pragma: no cover` (CI won't have the extra).
- **Never raises:** `build_repo_map` catches broker denial and per-file `OSError`; a parser exception inside `extract` would propagate out of the tool — but the tool is dispatched through the registry which converts it to a failed `ToolResult`. Still, keep `extract_python` defensive (missing nodes → `"?"`), and if the real parser can raise on odd input, wrap the `extract(...)` call in Task 3 in a `try/except Exception: continue` so one bad file can't abort the whole map. (Add that guard — it's within the never-crash contract.)
- **Additive** except: `tools.py` (one import + one call), `pyproject.toml` (extra + 2 mypy override lines). Nothing else. If more needs changing, stop and report.
- **No external-tool names** anywhere in code/docs (no "Aider", etc.).
