"""The set-working-directory tool: move Jack's active folder (grant-gated)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError, AccessPolicy
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("tools")


def set_working_directory(path: str, broker: AccessBroker, policy: AccessPolicy) -> str:
    """Set Jack's active folder; grants write access to a new folder on first use.

    Args:
        path: The folder path to switch to (absolute or relative to the current cwd).
        broker: The access broker that resolves and grants the path.
        policy: The access policy to update with the new cwd.

    Returns:
        A confirmation string (success) or a friendly error string (failure).
    """
    if not path or not path.strip():
        return "Tell me which folder to work in."
    try:
        folder = broker.ensure(path, write=True)  # prompts + grants on first use
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not folder.is_dir():
        return f"That's not a folder: {folder}"
    try:
        policy.set_cwd(folder)
    except (AccessDeniedError, Exception) as exc:  # never raise out of a tool handler
        _log.warning("set_working_directory failed: %s", exc)
        return f"I couldn't switch to that folder: {exc}"
    _log.info("active folder set via tool name=%r", folder.name)
    return f"Working in {folder.name} now ({folder})."


def register_workspace_tools(
    registry: ToolRegistry, broker: AccessBroker, policy: AccessPolicy
) -> None:
    """Register the set_working_directory tool.

    Args:
        registry: The tool registry to register into.
        broker: The access broker used to resolve and grant paths.
        policy: The access policy whose cwd will be updated.
    """
    registry.register(
        ToolSpec(
            name="set_working_directory",
            description=(
                "Set the ACTIVE folder Jack works in — where create_file/list_files/etc. "
                "operate by default. Use when the user says 'work in <folder>', 'switch to my "
                "<name> project', 'use this folder', 'set my workspace to <path>'. Pass the "
                "folder path; Jack asks to grant a new folder on first use."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the folder to work in."}
                },
                "required": ["path"],
            },
            handler=lambda path: set_working_directory(path, broker, policy),
            risk=Risk.WRITE,
            ack="Switching folder.",
        )
    )
    _log.info("workspace tool registered (set_working_directory)")
