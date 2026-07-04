r"""Code-editing tools for the coder profile (path-jailed, OS-neutral).

Code-oriented siblings of the assistant's ``fileio`` tools: ``read_file`` returns
``{n}\t{line}`` line-numbered text (so edits can cite lines), ``write_file`` is
**create-only** (never clobbers an existing file — that is what ``edit_file`` and, later,
checkpoints are for), and ``edit_file``/``multi_edit`` apply search/replace blocks via
:mod:`autobot.tools.code.edits`. Every path is resolved through the shared
:class:`~autobot.tools.access.AccessBroker`, so the workspace jail, folder grants, and
audit log apply exactly as they do for the assistant's file tools.
"""

from __future__ import annotations

from pathlib import Path

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.code.edits import apply_replace
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("coder")

_READ_CHAR_CAP = 100_000  # max chars returned into the conversation
_READ_LINE_CAP = 2000  # max lines returned in one read_file call


def _read_text(resolved: Path) -> tuple[str | None, str]:
    """Read a text file. Returns (text, "") on success or (None, error_message)."""
    if not resolved.exists():
        return None, f"There's no file at {resolved}."
    if resolved.is_dir():
        return None, f"'{resolved.name}' is a folder, not a file."
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        return None, f"I couldn't read {resolved.name}: {exc}"
    if b"\x00" in data[:4096]:
        return None, f"'{resolved.name}' looks like a binary file, so I can't read it as text."
    return data.decode("utf-8", errors="replace"), ""


def read_file(path: str, broker: AccessBroker, offset: int = 1, limit: int = 0) -> str:
    r"""Return a text file's contents, line-numbered (``{n}\t{line}``), bounded.

    Args:
        path: File path (relative paths resolve against the active folder).
        broker: The access broker enforcing the workspace jail and grants.
        offset: 1-based first line to return (values < 1 are treated as 1).
        limit: Max lines to return; 0 (default) means up to ``_READ_LINE_CAP``.
    """
    if not path:
        return "Which file should I read? Tell me its path."
    try:
        resolved = broker.ensure(path, write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    text, err = _read_text(resolved)
    if text is None:
        return err
    lines = text.split("\n")
    if lines and lines[-1] == "":  # a final newline yields a trailing "" — not a line
        lines = lines[:-1]
    total = len(lines)
    start = max(1, offset)
    count = limit if limit and limit > 0 else _READ_LINE_CAP
    window = lines[start - 1 : start - 1 + count]
    numbered = "\n".join(f"{start + idx}\t{line}" for idx, line in enumerate(window))
    if len(numbered) > _READ_CHAR_CAP:
        numbered = numbered[:_READ_CHAR_CAP] + "\n…(truncated)"
    shown = start - 1 + len(window)
    tail = f"\n…({total - shown} more line(s); read with a higher offset)" if shown < total else ""
    _log.info("read_file name=%r lines=%d offset=%d", resolved.name, len(window), start)
    return f"{resolved.name} (lines {start}-{shown} of {total}):\n{numbered}{tail}"


def write_file(path: str, content: str, broker: AccessBroker) -> str:
    """Create a NEW text file (gated; create-only — refuses to overwrite an existing one)."""
    if not path:
        return "Where should I save it? Tell me the file path."
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if resolved.exists():
        return (
            f"'{resolved.name}' already exists — use edit_file or multi_edit to change it "
            "(write_file only creates new files)."
        )
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"I couldn't write {resolved.name}: {exc}"
    n = len(content)
    _log.info("write_file name=%r chars=%d", resolved.name, n)
    return f"Wrote {n} character{'s' if n != 1 else ''} to {resolved.name}."


def edit_file(
    path: str, find: str, replace: str, broker: AccessBroker, replace_all: bool = False
) -> str:
    """Replace ``find`` with ``replace`` in an EXISTING file (gated). See :mod:`.edits`."""
    if not path:
        return "Which file should I edit? Tell me its path."
    if not find:
        return "Tell me the exact text to replace (a non-empty `find`)."
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    text, err = _read_text(resolved)
    if text is None:
        return err
    result = apply_replace(text, find, replace, replace_all=replace_all)
    if not result.ok:
        return f"I couldn't edit {resolved.name}: {result.detail}."
    try:
        resolved.write_text(result.content, encoding="utf-8")
    except OSError as exc:
        return f"I couldn't save {resolved.name}: {exc}"
    _log.info("edit_file name=%r detail=%r", resolved.name, result.detail)
    return f"Edited {resolved.name} ({result.detail})."


def multi_edit(path: str, edits: list[dict[str, str]] | None, broker: AccessBroker) -> str:
    """Apply a list of ``{find, replace}`` edits to one file, atomically (all-or-nothing).

    Edits apply in order to a working copy; the file is written only if every edit matches.
    A failure (bad shape, no match, ambiguous match, or a ``find`` that is a substring of an
    earlier edit's ``replace``) writes nothing and reports which edit failed, so a partial
    edit can never corrupt the file.
    """
    if not path:
        return "Which file should I edit? Tell me its path."
    if not edits:
        return "No edits to apply — pass a list of {find, replace} objects."
    if not isinstance(edits, list):  # untrusted JSON: a scalar would raise on iteration
        return "The `edits` value must be a list of {find, replace} objects."
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    text, err = _read_text(resolved)
    if text is None:
        return err
    working = text
    applied_replacements: list[str] = []
    for idx, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict) or "find" not in edit or "replace" not in edit:
            return f"Edit {idx} is malformed — each edit needs a `find` and a `replace`."
        find, replace = edit["find"], edit["replace"]
        if not isinstance(find, str) or not isinstance(replace, str) or not find:
            return f"Edit {idx} is malformed — `find` and `replace` must be text, `find` non-empty."
        probe = find.rstrip("\n")
        if probe and any(probe in prev for prev in applied_replacements):
            return (
                f"Edit {idx}'s search text was produced by an earlier edit; "
                "combine them into one edit or reorder them."
            )
        result = apply_replace(working, find, replace)
        if not result.ok:
            return f"Edit {idx} didn't apply ({result.detail}); nothing was changed."
        working = result.content
        applied_replacements.append(replace)
    try:
        resolved.write_text(working, encoding="utf-8")
    except OSError as exc:
        return f"I couldn't save {resolved.name}: {exc}"
    n = len(edits)
    _log.info("multi_edit name=%r edits=%d", resolved.name, n)
    return f"Applied {n} edit{'s' if n != 1 else ''} to {resolved.name}."


def register_code_tools(registry: ToolRegistry, broker: AccessBroker) -> None:
    """Register the coder-profile code tools (read/write/edit/multi_edit).

    All are gated (``core=False``) — advertised only when the tool selector judges them
    relevant — and route every path through ``broker`` for the workspace jail. The coder
    profile wires this in a later change (#53).
    """
    registry.register(
        ToolSpec(
            name="read_file",
            description=(
                "Read a source file's contents, line-numbered, so you can cite lines when "
                "editing. Cues: 'read/open/show X', 'what's in X'. Pass the file path; use "
                "`offset` (1-based first line) and `limit` (line count) to page through a large "
                "file. Read a file before editing it — edit_file matches against its current "
                "contents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to read."},
                    "offset": {"type": "integer", "description": "1-based first line (optional)."},
                    "limit": {"type": "integer", "description": "Max lines to return (optional)."},
                },
                "required": ["path"],
            },
            handler=lambda path="", offset=1, limit=0: read_file(path, broker, offset, limit),
            risk=Risk.READ_ONLY,
            ack="Reading that file.",
        )
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description=(
                "Create a NEW source file with the given content (it makes missing parent "
                "folders). This is create-only: it will NOT overwrite an existing file — to "
                "change an existing file use edit_file or multi_edit. Cues: 'create X', 'add a "
                "new file X'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the new file."},
                    "content": {"type": "string", "description": "The full text to write."},
                },
                "required": ["path", "content"],
            },
            handler=lambda path="", content="": write_file(path, content, broker),
            risk=Risk.WRITE,
            ack="Writing that file.",
        )
    )
    registry.register(
        ToolSpec(
            name="edit_file",
            description=(
                "Change an EXISTING file by replacing `find` with `replace`. `find` must "
                "uniquely identify one place — include enough surrounding lines to be "
                "unambiguous — unless you set `replace_all` to change every occurrence (e.g. "
                "renaming a symbol). Matching tolerates trailing-whitespace drift. Cues: "
                "'change/replace/fix A to B in X'. For several edits to one file, use multi_edit."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to edit."},
                    "find": {"type": "string", "description": "The text to find (non-empty)."},
                    "replace": {"type": "string", "description": "The replacement text."},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence (default false).",
                    },
                },
                "required": ["path", "find", "replace"],
            },
            handler=lambda path="", find="", replace="", replace_all=False: edit_file(
                path, find, replace, broker, replace_all
            ),
            risk=Risk.WRITE,
            ack="Editing that file.",
        )
    )
    registry.register(
        ToolSpec(
            name="multi_edit",
            description=(
                "Apply several find/replace edits to ONE file in a single atomic step — if any "
                "edit doesn't match, none are applied. Pass `edits` as a list of {find, replace} "
                "objects; they apply in order, each seeing the previous edit's result. Use this "
                "instead of repeated edit_file calls on the same file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to edit."},
                    "edits": {
                        "type": "array",
                        "description": "Edits applied in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string"},
                                "replace": {"type": "string"},
                            },
                            "required": ["find", "replace"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
            handler=lambda path="", edits=None: multi_edit(path, edits, broker),
            risk=Risk.WRITE,
            ack="Editing that file.",
        )
    )
    _log.info("code tools registered (read_file/write_file/edit_file/multi_edit)")
