"""Read, copy, and write files anywhere the user has granted access.

These are the broad-scope file tools — distinct from the workspace-jailed
``filesystem`` tools. Every path goes through the central :class:`AccessPolicy` via
an :class:`AccessBroker`, which asks the user to grant a folder on first use.

Privacy note: ``read_file_text`` puts the file's contents into the conversation, so
in cloud mode they go to the LLM provider; ``copy_file_to_clipboard`` keeps contents
out of the model (safe even in cloud mode). Both are gated by the access policy.
"""

from __future__ import annotations

from pathlib import Path

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.clipboard import Runner as ClipRunner
from autobot.tools.clipboard import set_clipboard
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("tools")

_MODEL_READ_CAP = 20_000  # chars returned into the conversation
_CLIP_READ_CAP = 200_000  # chars copied to the clipboard / edited


def _read_text(resolved: Path, cap: int) -> tuple[str | None, str]:
    """Read a text file, capped. Returns (text, "") or (None, error_message)."""
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
    text = data.decode("utf-8", errors="replace")
    return (text if len(text) <= cap else text[:cap] + "\n…(truncated)"), ""


def read_file_text(path: str, broker: AccessBroker) -> str:
    """Return a text file's contents (gated; bounded for the conversation)."""
    if not path:
        return "Which file should I read? Tell me its path."
    try:
        resolved = broker.ensure(path, write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    text, err = _read_text(resolved, _MODEL_READ_CAP)
    if text is None:
        return err
    _log.info("read_file_text name=%r chars=%d", resolved.name, len(text))
    return f"Contents of {resolved.name}:\n{text}"


def copy_file_to_clipboard(
    path: str, broker: AccessBroker, clip_runner: ClipRunner | None = None
) -> str:
    """Read a file and put its contents on the clipboard — without showing them here."""
    if not path:
        return "Which file should I copy? Tell me its path."
    try:
        resolved = broker.ensure(path, write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    text, err = _read_text(resolved, _CLIP_READ_CAP)
    if text is None:
        return err
    msg = set_clipboard(text, clip_runner)
    if msg.startswith("I couldn't"):
        return msg
    _log.info("copy_file_to_clipboard name=%r chars=%d", resolved.name, len(text))
    return f"Copied {len(text)} characters from {resolved.name} to your clipboard."


def write_file(path: str, content: str, broker: AccessBroker) -> str:
    """Create or overwrite a text file (gated; creates parent folders as needed)."""
    if not path:
        return "Where should I save it? Tell me the file path."
    if not content:
        return "I didn't get any text to write — include the file's content and try again."
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    body = content
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)  # create folders within the grant
        resolved.write_text(body, encoding="utf-8")
    except OSError as exc:
        return f"I couldn't write {resolved.name}: {exc}"
    n = len(body)
    _log.info("write_file name=%r chars=%d", resolved.name, n)
    return f"Wrote {n} character{'s' if n != 1 else ''} to {resolved.name}."


def edit_file(path: str, find: str, replace: str, broker: AccessBroker) -> str:
    """Replace exact text in a file (gated). Replaces every occurrence of ``find``."""
    if not path:
        return "Which file should I edit? Tell me its path."
    if not find:
        return "Tell me the exact text to replace."
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    text, err = _read_text(resolved, _CLIP_READ_CAP)
    if text is None:
        return err
    count = text.count(find)
    if count == 0:
        return f"I couldn't find that text in {resolved.name}, so nothing changed."
    try:
        resolved.write_text(text.replace(find, replace or ""), encoding="utf-8")
    except OSError as exc:
        return f"I couldn't save {resolved.name}: {exc}"
    _log.info("edit_file name=%r replacements=%d", resolved.name, count)
    return f"Edited {resolved.name} ({count} replacement{'s' if count != 1 else ''})."


def register_file_io_tools(
    registry: ToolRegistry, broker: AccessBroker, clip_runner: ClipRunner | None = None
) -> None:
    """Register read/copy/write/edit tools that go through the access policy."""
    registry.register(
        ToolSpec(
            name="read_file_text",
            description=(
                "Read a text file's contents from anywhere on the Mac. Cues: 'read X', "
                "'show me the contents of X', 'summarize/explain this file'. Find the file "
                "first with search_files, then pass its full path. Jack asks to grant the "
                "folder on first use. To put a file's contents on the clipboard WITHOUT "
                "reading them into the conversation, use copy_file_to_clipboard instead."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Full path to the file."}},
                "required": ["path"],
            },
            handler=lambda path="": read_file_text(path, broker),
            risk=Risk.READ_ONLY,
            ack="Reading that file.",
        )
    )
    registry.register(
        ToolSpec(
            name="copy_file_to_clipboard",
            description=(
                "Read a file and put its full contents on the clipboard so the user can "
                "paste it elsewhere (e.g. into another app). Cues: 'copy the contents of X', "
                "'copy that file so I can paste it'. The contents are NOT shown in the chat. "
                "Pass the file's full path."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Full path to the file."}},
                "required": ["path"],
            },
            handler=lambda path="": copy_file_to_clipboard(path, broker, clip_runner),
            risk=Risk.WRITE,
            ack="Copying that file.",
        )
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description=(
                "Create or overwrite a text file with the given content, anywhere in the "
                "user's folders (it creates any missing parent folders, and asks to grant "
                "write access on first use). Use THIS — not create_file — for the user's own "
                "files; create_file only writes to Jack's private scratch workspace. Cues: "
                "'create a file X with …', 'save this to X', 'write … to X'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full path to the file to write."},
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
                "Edit an EXISTING text file by replacing exact text with new text (every "
                "occurrence). The `find` text must already be in the file — never pass an "
                "empty `find`. To create a file or replace its whole contents, use write_file "
                "instead. Cues: 'in X, change A to B', 'replace A with B in that file'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full path to the file to edit."},
                    "find": {"type": "string", "description": "Exact text to replace."},
                    "replace": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "find", "replace"],
            },
            handler=lambda path="", find="", replace="": edit_file(path, find, replace, broker),
            risk=Risk.WRITE,
            ack="Editing that file.",
        )
    )
    _log.info("file I/O tools registered (read/copy/write/edit)")
