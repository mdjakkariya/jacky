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

import difflib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autobot.core.streaming import output_sink
from autobot.core.types import ErrorCategory, Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.code.diagnostics import register_diagnostics_tool
from autobot.tools.code.edits import apply_replace
from autobot.tools.code.plan import register_plan_tool
from autobot.tools.code.rename import register_rename_tool
from autobot.tools.code.repomap import register_repomap_tool
from autobot.tools.code.search import register_nav_tools
from autobot.tools.code.shell import register_exec_tools
from autobot.tools.code.symbol_nav import register_symbol_tool
from autobot.tools.registry import ToolFailure, ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from autobot.tasks import NotificationInbox, TaskRegistry

_log = get_logger("coder")

_READ_CHAR_CAP = 100_000  # max chars returned into the conversation
_READ_LINE_CAP = 2000  # max lines returned in one read_file call
_MAX_DIFF_LINES = 160  # cap the diff streamed to the UI so a big rewrite can't flood the view
_MULTI_READ_MAX_FILES = 20  # max files per read_files call
_MULTI_READ_PER_FILE_LINES = 400  # lines returned per file in a multi-file read
_MULTI_READ_CHAR_CAP = 100_000  # total chars across a read_files call


def _emit_diff(old: str, new: str, name: str) -> None:
    """Stream a unified diff of an edit to the UI (via ``output_sink``) for inline review.

    A no-op when no turn is streaming (``output_sink`` unset) or nothing changed. Capped at
    :data:`_MAX_DIFF_LINES` lines so a large rewrite shows a bounded, reviewable hunk.
    """
    sink = output_sink.get()
    if sink is None or old == new:
        return
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile=name, tofile=name, lineterm=""
    )
    for i, line in enumerate(diff):
        if i >= _MAX_DIFF_LINES:
            sink(f"… (diff truncated at {_MAX_DIFF_LINES} lines)")
            break
        sink(line)


def _read_text(resolved: Path) -> tuple[str | None, str, str]:
    """Read a text file.

    Returns ``(text, "", "")`` on success or ``(None, error_message, category)`` on
    failure, where ``category`` is an :class:`~autobot.core.types.ErrorCategory` value.
    """
    if not resolved.exists():
        return None, f"There's no file at {resolved}.", ErrorCategory.NOT_FOUND
    if resolved.is_dir():
        return None, f"'{resolved.name}' is a folder, not a file.", ErrorCategory.INVALID
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        return None, f"I couldn't read {resolved.name}: {exc}", ErrorCategory.UNREADABLE
    if b"\x00" in data[:4096]:
        return (
            None,
            f"'{resolved.name}' looks like a binary file, so I can't read it as text.",
            ErrorCategory.UNREADABLE,
        )
    return data.decode("utf-8", errors="replace"), "", ""


def read_file(path: str, broker: AccessBroker, offset: int = 1, limit: int = 0) -> str:
    r"""Return a text file's contents, line-numbered (``{n}\t{line}``), bounded.

    Args:
        path: File path (relative paths resolve against the active folder).
        broker: The access broker enforcing the workspace jail and grants.
        offset: 1-based first line to return (values < 1 are treated as 1).
        limit: Max lines to return; 0 (default) means up to ``_READ_LINE_CAP``.
    """
    if not path:
        return ToolFailure("Which file should I read? Tell me its path.", ErrorCategory.INVALID)
    try:
        resolved = broker.ensure(path, write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return ToolFailure(str(exc), ErrorCategory.DENIED)
    text, err, cat = _read_text(resolved)
    if text is None:
        return ToolFailure(err, cat)
    broker.mark_read(resolved)  # read-before-edit: remember we've seen this file's contents
    lines = text.split("\n")
    if lines and lines[-1] == "":  # a final newline yields a trailing "" — not a line
        lines = lines[:-1]
    total = len(lines)
    start = max(1, offset)
    count = limit if limit and limit > 0 else _READ_LINE_CAP
    window = lines[start - 1 : start - 1 + count]
    numbered_lines = [f"{start + idx}\t{line}" for idx, line in enumerate(window)]
    # Keep WHOLE lines within the char cap so the resume offset below is accurate (the first
    # line is always kept, even if it alone exceeds the cap — handled just after).
    used = 0
    kept = 0
    for ln in numbered_lines:
        if kept and used + len(ln) + 1 > _READ_CHAR_CAP:
            break
        used += len(ln) + 1
        kept += 1
    numbered = "\n".join(numbered_lines[:kept])
    if len(numbered) > _READ_CHAR_CAP:  # a single over-long line — hard-cap it as a backstop
        numbered = numbered[:_READ_CHAR_CAP] + "\n…(line truncated at the character cap)"
    shown = start - 1 + kept  # lines actually returned (the char cap may stop short of the window)
    tail = (
        f"\n…({total - shown} more line(s); continue with offset {shown + 1})"
        if shown < total
        else ""
    )
    _log.info("read_file name=%r lines=%d offset=%d", resolved.name, kept, start)
    return f"{resolved.name} (lines {start}-{shown} of {total}):\n{numbered}{tail}"


def read_files(paths: list[str] | None, broker: AccessBroker) -> str:
    """Read several files in one call (gated), each line-numbered and bounded.

    A convenience over many ``read_file`` calls: each path is read (up to
    ``_MULTI_READ_PER_FILE_LINES`` lines) and the blocks are concatenated within a total
    character budget. A file that can't be read shows its error inline rather than failing the
    whole call; per-file reads still count toward the read-before-edit set.
    """
    if not paths:
        return ToolFailure(
            "Which files should I read? Pass a list of paths.", ErrorCategory.INVALID
        )
    if not isinstance(paths, list):  # untrusted JSON: a scalar would iterate by character
        return ToolFailure("`paths` must be a list of file paths.", ErrorCategory.INVALID)
    blocks: list[str] = []
    used = 0
    for raw in paths[:_MULTI_READ_MAX_FILES]:
        block = read_file(str(raw), broker, offset=1, limit=_MULTI_READ_PER_FILE_LINES)
        blocks.append(block)
        used += len(block)
        if used > _MULTI_READ_CHAR_CAP:
            blocks.append("…(remaining files not shown — read them individually)")
            break
    if len(paths) > _MULTI_READ_MAX_FILES:
        blocks.append(
            f"…({len(paths) - _MULTI_READ_MAX_FILES} more path(s) not read; cap is "
            f"{_MULTI_READ_MAX_FILES} per call)"
        )
    _log.info("read_files count=%d", min(len(paths), _MULTI_READ_MAX_FILES))
    return "\n\n".join(blocks)


def delete_file(path: str, broker: AccessBroker) -> str:
    """Delete a single file (gated, DESTRUCTIVE). Refuses folders — no recursive removal."""
    if not path:
        return ToolFailure("Which file should I delete? Tell me its path.", ErrorCategory.INVALID)
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return ToolFailure(str(exc), ErrorCategory.DENIED)
    if not resolved.exists():
        return ToolFailure(f"There's no file at {resolved}.", ErrorCategory.NOT_FOUND)
    if resolved.is_dir():
        return ToolFailure(
            f"'{resolved.name}' is a folder — I only delete single files, not directories.",
            ErrorCategory.INVALID,
        )
    try:
        resolved.unlink()
    except OSError as exc:
        return ToolFailure(f"I couldn't delete {resolved.name}: {exc}", ErrorCategory.UNREADABLE)
    _log.info("delete_file name=%r", resolved.name)
    return f"Deleted {resolved.name}."


def move_file(source: str, dest: str, broker: AccessBroker) -> str:
    """Move or rename a file within the workspace (gated, DESTRUCTIVE). Won't overwrite ``dest``."""
    if not source or not dest:
        return ToolFailure("Tell me both a source and a destination path.", ErrorCategory.INVALID)
    try:
        src = broker.ensure(source, write=True)
        dst = broker.ensure(dest, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return ToolFailure(str(exc), ErrorCategory.DENIED)
    if not src.exists():
        return ToolFailure(f"There's no file at {src}.", ErrorCategory.NOT_FOUND)
    if dst.exists():
        return ToolFailure(
            f"'{dst.name}' already exists — move refused so nothing is overwritten.",
            ErrorCategory.EXISTS,
        )
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return ToolFailure(f"I couldn't move {src.name}: {exc}", ErrorCategory.UNREADABLE)
    _log.info("move_file src=%r dst=%r", src.name, dst.name)
    return f"Moved {src.name} → {dst.name}."


def write_file(path: str, content: str, broker: AccessBroker) -> str:
    """Create a NEW text file (gated; create-only — refuses to overwrite an existing one)."""
    if not path:
        return ToolFailure("Where should I save it? Tell me the file path.", ErrorCategory.INVALID)
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return ToolFailure(str(exc), ErrorCategory.DENIED)
    if resolved.exists():
        return ToolFailure(
            f"'{resolved.name}' already exists — use edit_file or multi_edit to change it "
            "(write_file only creates new files).",
            ErrorCategory.EXISTS,
        )
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolFailure(f"I couldn't write {resolved.name}: {exc}", ErrorCategory.UNREADABLE)
    _emit_diff("", content, resolved.name)  # new file → all-additions diff for inline review
    n = len(content)
    _log.info("write_file name=%r chars=%d", resolved.name, n)
    return f"Wrote {n} character{'s' if n != 1 else ''} to {resolved.name}."


def edit_file(
    path: str, find: str, replace: str, broker: AccessBroker, replace_all: bool = False
) -> str:
    """Replace ``find`` with ``replace`` in an EXISTING file (gated). See :mod:`.edits`."""
    if not path:
        return ToolFailure("Which file should I edit? Tell me its path.", ErrorCategory.INVALID)
    if not find:
        return ToolFailure(
            "Tell me the exact text to replace (a non-empty `find`).", ErrorCategory.INVALID
        )
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return ToolFailure(str(exc), ErrorCategory.DENIED)
    text, err, cat = _read_text(resolved)
    if text is None:
        return ToolFailure(err, cat)
    result = apply_replace(text, find, replace, replace_all=replace_all)
    if not result.ok:
        detail = result.detail
        if result.category == ErrorCategory.NOT_FOUND and not broker.was_read(resolved):
            detail += "; read the file first so your search text matches its current contents"
        return ToolFailure(f"I couldn't edit {resolved.name}: {detail}.", result.category)
    try:
        resolved.write_text(result.content, encoding="utf-8")
    except OSError as exc:
        return ToolFailure(f"I couldn't save {resolved.name}: {exc}", ErrorCategory.UNREADABLE)
    _emit_diff(text, result.content, resolved.name)  # inline diff of what changed
    _log.info("edit_file name=%r detail=%r", resolved.name, result.detail)
    return f"Edited {resolved.name} ({result.detail})."


def _apply_edit_list(
    working: str,
    edits: list[dict[str, str]],
    broker: AccessBroker,
    resolved: Path,
    *,
    label: str,
) -> tuple[str | None, str, str]:
    """Apply an ordered list of ``{find, replace}`` edits to ``working`` (in memory, no I/O).

    Returns ``(new_text, "", "")`` on success or ``(None, message, category)`` on the first
    failure — a bad shape, no/ambiguous match, or a ``find`` that a previous edit's ``replace``
    produced. ``label`` prefixes failure messages (e.g. ``"Edit"`` → ``"Edit 2 didn't apply …"``).
    Shared by :func:`multi_edit` (one file) and :func:`multi_patch` (many files).
    """
    applied: list[str] = []
    for idx, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict) or "find" not in edit or "replace" not in edit:
            return (
                None,
                f"{label} {idx} is malformed — needs a `find` and a `replace`.",
                ErrorCategory.INVALID,
            )
        find, replace = edit["find"], edit["replace"]
        if not isinstance(find, str) or not isinstance(replace, str) or not find:
            return (
                None,
                f"{label} {idx} is malformed — `find`/`replace` must be text, `find` non-empty.",
                ErrorCategory.INVALID,
            )
        probe = find.rstrip("\n")
        if probe and any(probe in prev for prev in applied):
            return (
                None,
                (
                    f"{label} {idx}'s search text was produced by an earlier edit; "
                    "combine them into one edit or reorder them."
                ),
                ErrorCategory.INVALID,
            )
        result = apply_replace(working, find, replace)
        if not result.ok:
            detail = result.detail
            if result.category == ErrorCategory.NOT_FOUND and not broker.was_read(resolved):
                detail += "; read the file first so your search text matches its current contents"
            return (
                None,
                f"{label} {idx} didn't apply ({detail}); nothing was changed.",
                result.category,
            )
        working = result.content
        applied.append(replace)
    return working, "", ""


def multi_edit(path: str, edits: list[dict[str, str]] | None, broker: AccessBroker) -> str:
    """Apply a list of ``{find, replace}`` edits to one file, atomically (all-or-nothing).

    Edits apply in order to a working copy; the file is written only if every edit matches.
    A failure (bad shape, no match, ambiguous match, or a ``find`` that is a substring of an
    earlier edit's ``replace``) writes nothing and reports which edit failed, so a partial
    edit can never corrupt the file.
    """
    if not path:
        return ToolFailure("Which file should I edit? Tell me its path.", ErrorCategory.INVALID)
    if not edits:
        return ToolFailure(
            "No edits to apply — pass a list of {find, replace} objects.", ErrorCategory.INVALID
        )
    if not isinstance(edits, list):  # untrusted JSON: a scalar would raise on iteration
        return ToolFailure(
            "The `edits` value must be a list of {find, replace} objects.", ErrorCategory.INVALID
        )
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return ToolFailure(str(exc), ErrorCategory.DENIED)
    text, err, cat = _read_text(resolved)
    if text is None:
        return ToolFailure(err, cat)
    working, err, cat = _apply_edit_list(text, edits, broker, resolved, label="Edit")
    if working is None:
        return ToolFailure(err, cat)
    try:
        resolved.write_text(working, encoding="utf-8")
    except OSError as exc:
        return ToolFailure(f"I couldn't save {resolved.name}: {exc}", ErrorCategory.UNREADABLE)
    _emit_diff(text, working, resolved.name)  # inline diff of the combined edits
    n = len(edits)
    _log.info("multi_edit name=%r edits=%d", resolved.name, n)
    return f"Applied {n} edit{'s' if n != 1 else ''} to {resolved.name}."


def multi_patch(files: list[dict[str, Any]] | None, broker: AccessBroker) -> str:
    """Apply edits across SEVERAL files atomically — validate every file, then write, or none.

    ``files`` is a list of ``{path, edits}`` where ``edits`` is a list of ``{find, replace}``.
    Every file's edits are applied to an in-memory copy first; only if *all* succeed are the
    files written — so a bad match in one file leaves the whole set untouched. Use it for a
    coordinated change spanning multiple files (a rename across call sites, say). (A rare OS
    error mid-write is covered by the turn's checkpoint.)
    """
    if not files:
        return ToolFailure(
            "No files to patch — pass a list of {path, edits}.", ErrorCategory.INVALID
        )
    if not isinstance(files, list):
        return ToolFailure(
            "`files` must be a list of {path, edits} objects.", ErrorCategory.INVALID
        )
    planned: list[tuple[Path, str, str]] = []  # (resolved, new_text, original)
    for fi, spec in enumerate(files, start=1):
        if not isinstance(spec, dict) or "path" not in spec or "edits" not in spec:
            return ToolFailure(
                f"File {fi} is malformed — needs `path` and `edits`.", ErrorCategory.INVALID
            )
        path, edits = spec["path"], spec["edits"]
        if not isinstance(path, str) or not path:
            return ToolFailure(f"File {fi} needs a non-empty `path`.", ErrorCategory.INVALID)
        if not isinstance(edits, list) or not edits:
            return ToolFailure(
                f"File {fi} ({path}) needs a non-empty `edits` list.", ErrorCategory.INVALID
            )
        try:
            resolved = broker.ensure(path, write=True)
        except (AccessDeniedError, PermissionError) as exc:
            return ToolFailure(str(exc), ErrorCategory.DENIED)
        text, err, cat = _read_text(resolved)
        if text is None:
            return ToolFailure(err, cat)
        new_text, err, cat = _apply_edit_list(
            text, edits, broker, resolved, label=f"{resolved.name} edit"
        )
        if new_text is None:
            return ToolFailure(f"{err} No files were changed.", cat)
        planned.append((resolved, new_text, text))
    written: list[str] = []
    for resolved, new_text, original in planned:  # every file validated → write them all
        try:
            resolved.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return ToolFailure(
                f"I couldn't save {resolved.name}: {exc} (earlier files may already be written — "
                "use the checkpoint to roll back).",
                ErrorCategory.UNREADABLE,
            )
        _emit_diff(original, new_text, resolved.name)
        written.append(resolved.name)
    _log.info("multi_patch files=%d", len(written))
    return f"Patched {len(written)} file(s): {', '.join(written)}."


def register_code_tools(
    registry: ToolRegistry,
    broker: AccessBroker,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
    output_model_cap: int = 10_000,
    task_registry: TaskRegistry | None = None,
    task_inbox: NotificationInbox | None = None,
) -> None:
    """Register the coder-profile code tools (read/write/edit/multi_edit).

    All are gated (``core=False``) — advertised only when the tool selector judges them
    relevant — and route every path through ``broker`` for the workspace jail. The coder
    profile wires this in a later change (#53).

    Args:
        registry: Tool registry to register into.
        broker: Access broker enforcing the workspace jail.
        allowlist: Commands pre-approved to run without confirmation, forwarded to
            ``run_command`` via :func:`register_exec_tools`.
        blocklist: Commands always blocked, forwarded to ``run_command`` via
            :func:`register_exec_tools`.
        output_model_cap: Max chars of command output returned to the model inline,
            forwarded to ``run_command`` via :func:`register_exec_tools`.
        task_registry: Process-global async-task registry, forwarded to
            :func:`register_exec_tools` to enable ``run_command``'s background path.
        task_inbox: Per-session notification inbox, forwarded alongside ``task_registry``.
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
            name="read_files",
            description=(
                "Read several files at once — pass `paths` (a list). Use this instead of many "
                "read_file calls when you want to see a handful of files together (each is "
                "line-numbered and bounded). For paging through one large file, use read_file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths to read.",
                    },
                },
                "required": ["paths"],
            },
            handler=lambda paths=None: read_files(paths, broker),
            risk=Risk.READ_ONLY,
            ack="Reading those files.",
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
    registry.register(
        ToolSpec(
            name="multi_patch",
            description=(
                "Apply edits across SEVERAL files in one atomic step — if any edit in any file "
                "doesn't match, nothing is written. Pass `files`: a list of {path, edits} where "
                "edits is a list of {find, replace}. Use for a coordinated change spanning files "
                "(e.g. a rename across call sites); for one file use multi_edit."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "description": "Per-file patches, applied all-or-nothing.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "edits": {
                                    "type": "array",
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
                    },
                },
                "required": ["files"],
            },
            handler=lambda files=None: multi_patch(files, broker),
            risk=Risk.WRITE,
            ack="Editing several files.",
        )
    )
    registry.register(
        ToolSpec(
            name="delete_file",
            description=(
                "Delete a single file (not a folder). Use when a file should be removed as part "
                "of the task. Prefer this over `run_command rm` — it's path-jailed and confirmed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path of the file to delete."},
                },
                "required": ["path"],
            },
            handler=lambda path="": delete_file(path, broker),
            risk=Risk.DESTRUCTIVE,
            ack="Deleting that file.",
        )
    )
    registry.register(
        ToolSpec(
            name="move_file",
            description=(
                "Move or rename a file within the workspace. Won't overwrite an existing "
                "destination. Prefer this over `run_command mv` — it's path-jailed and confirmed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Current path of the file."},
                    "dest": {"type": "string", "description": "New path (move or rename)."},
                },
                "required": ["source", "dest"],
            },
            handler=lambda source="", dest="": move_file(source, dest, broker),
            risk=Risk.DESTRUCTIVE,
            ack="Moving that file.",
        )
    )
    _log.info("code tools registered (read/write/edit/multi_edit/delete/move/update_plan)")
    register_nav_tools(registry, broker)
    register_exec_tools(
        registry,
        broker,
        allowlist=allowlist,
        blocklist=blocklist,
        output_model_cap=output_model_cap,
        task_registry=task_registry,
        task_inbox=task_inbox,
    )
    register_repomap_tool(registry, broker)
    lsp_manager = register_symbol_tool(registry, broker)
    register_rename_tool(registry, broker, lsp_manager)  # shares the language servers
    register_diagnostics_tool(registry, broker, lsp_manager)
    register_plan_tool(registry)
