"""The permission gate: the single choke point for executing tool calls.

Every tool the model wants to run passes through :meth:`PermissionGate.execute`,
which:

1. classifies the call by the tool's :class:`~autobot.core.types.Risk`,
2. asks for confirmation when the risk meets the configured threshold
   (destructive-only by default),
3. dispatches to the registry only if allowed, and
4. records the decision and outcome to the audit log — always, allowed or not.

The gate never trusts the model: an unregistered tool is denied outright, and a
declined confirmation aborts without side effects. UI is injected via the
:class:`Confirmer` protocol so the gate stays testable and headless.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from autobot.core.types import Decision, Risk, ToolCall, ToolResult
from autobot.logging_setup import get_logger
from autobot.mcp.adapter import split_namespaced
from autobot.tools.audit import AuditLog
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("gate")


@runtime_checkable
class Confirmer(Protocol):
    """Asks the user to approve an action and returns their answer.

    ``kind`` ("read" / "write" / "danger" / "network") lets the UI tier the card's
    tone so a benign read grant doesn't look like a destructive action; "network"
    marks an off-device send (the orange "data path" card).
    """

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        """Return ``True`` to proceed, ``False`` to cancel."""
        ...

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        """Pick one option's value (e.g. an access level); "" means cancel."""
        ...

    def confirm_action(self, prompt: str, kind: str = "danger") -> str:
        """Confirm a gated action: "once" (proceed), "session" (proceed + remember), "" (cancel)."""
        ...


class TerminalConfirmer:
    """Confirms via a ``[y/N]`` prompt on the terminal (default: No)."""

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        """Ask on stdin; only an explicit ``y``/``yes`` proceeds."""
        answer = input(f"{prompt} [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        """Confirm on stdin; a yes grants the least-privilege ``default``."""
        return default if self.confirm(prompt) else ""

    def confirm_action(self, prompt: str, kind: str = "danger") -> str:
        """Terminal has no session button: a yes proceeds once, anything else cancels."""
        return "once" if self.confirm(prompt) else ""


class AlwaysAllow:
    """A confirmer that approves everything (tests / non-interactive use)."""

    def confirm(self, prompt: str, kind: str = "danger") -> bool:  # noqa: D102
        return True

    def choose(  # noqa: D102
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default

    def confirm_action(self, prompt: str, kind: str = "danger") -> str:  # noqa: D102
        return "once"


class AlwaysDeny:
    """A confirmer that rejects everything (tests)."""

    def confirm(self, prompt: str, kind: str = "danger") -> bool:  # noqa: D102
        return False

    def choose(  # noqa: D102
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return ""

    def confirm_action(self, prompt: str, kind: str = "danger") -> str:  # noqa: D102
        return ""


class PermissionGate:
    """Guards tool execution with risk classification, confirmation, and audit."""

    def __init__(
        self,
        registry: ToolRegistry,
        audit: AuditLog,
        confirmer: Confirmer,
        confirm_at_or_above: Risk = Risk.DESTRUCTIVE,
        permission_status: Callable[[str], str] | None = None,
        on_permission_needed: Callable[[str], object] | None = None,
        scope_of: Callable[[ToolCall], str] | None = None,
    ) -> None:
        self._registry = registry
        self._audit = audit
        self._confirmer = confirmer
        self._threshold = confirm_at_or_above
        # Injected so the gate stays headless/testable. ``permission_status`` returns
        # "granted"/"needed"/"unknown" for a permission key; ``on_permission_needed``
        # is called (e.g. to open the Settings pane) when a tool is blocked.
        self._permission_status = permission_status
        self._on_permission_needed = on_permission_needed
        # Derives a per-call scope string (e.g. the target folder) for session grants;
        # None -> tool-name-only scope. Set in the composition root (app.build).
        self._scope_of = scope_of
        # Actions the user approved "for this session" (in-memory; key = "tool|scope").
        # Cleared on New Chat and on process restart.
        self._session_grants: set[str] = set()

    def risk_of(self, name: str) -> Risk | None:
        """The risk level of a registered tool, or ``None`` if it's unknown.

        Lets callers (e.g. the orchestrator's spoken acknowledgement) tailor
        behavior to whether a tool merely reads or actually acts.
        """
        spec = self._registry.get(name)
        return spec.risk if spec is not None else None

    def ack_of(self, name: str) -> str | None:
        """This tool's spoken-ack hint: text, ``""`` (silent), or ``None`` (use a pool).

        Lets the orchestrator say something that fits the action ("Opening that")
        rather than a generic risk-based filler — and stay quiet for tools like
        dismiss where a filler would be jarring.
        """
        spec = self._registry.get(name)
        return spec.ack if spec is not None else None

    def clear_session_grants(self) -> None:
        """Forget every "allow this session" grant (called on New Chat / restart)."""
        self._session_grants.clear()

    def _grant_key(self, call: ToolCall) -> str:
        """The session-grant key for a call: ``"{tool}|{scope}"`` (scope may be empty)."""
        scope = self._scope_of(call) if self._scope_of is not None else ""
        return f"{call.name}|{scope}"

    def _confirm_action(self, prompt: str, kind: str) -> str:
        """Ask the confirmer for a tri-state decision, falling back to bool confirm()."""
        fn = getattr(self._confirmer, "confirm_action", None)
        if callable(fn):
            decision: str = fn(prompt, kind)
            return decision if decision in ("once", "session") else ""
        return "once" if self._confirmer.confirm(prompt, kind) else ""

    def execute(self, call: ToolCall, *, pre_authorized: bool = False) -> ToolResult:
        """Run one tool call through the gate; see the module docstring for policy.

        Args:
            call: The tool call to run.
            pre_authorized: When ``True``, skip the confirmation prompt (the caller has
                already obtained the user's approval, e.g. an allowlisted command in an
                approved plan). Classification, dispatch, and auditing still happen, so
                the gate remains the single execution + audit choke point.
        """
        spec = self._registry.get(call.name)

        if spec is None:
            _log.warning("denied tool=%s reason=unknown_tool", call.name)
            self._audit.log(
                tool=call.name,
                arguments=call.arguments,
                risk="unknown",
                decision=Decision.DENIED,
                ok=None,
                detail="unknown tool",
            )
            return ToolResult(name=call.name, content=f"unknown tool: {call.name!r}", ok=False)

        # Permission gate: if this tool needs a macOS permission we know is missing,
        # refuse before running and surface the right Settings pane — rather than
        # letting it fail opaquely deep in AppleScript.
        if spec.requires and self._permission_status is not None:
            from autobot import permissions

            if self._permission_status(spec.requires) == permissions.NEEDED:
                _log.info(
                    "denied tool=%s reason=permission_needed perm=%s", call.name, spec.requires
                )
                if self._on_permission_needed is not None:
                    self._on_permission_needed(spec.requires)
                self._audit.log(
                    tool=call.name,
                    arguments=call.arguments,
                    risk=spec.risk.name,
                    decision=Decision.DENIED,
                    ok=None,
                    detail=f"missing permission: {spec.requires}",
                )
                return ToolResult(
                    name=call.name, content=permissions.needed_message(spec.requires), ok=False
                )

        needs_confirm = spec.risk >= self._threshold or (spec.network and spec.risk >= Risk.WRITE)
        if needs_confirm and pre_authorized:
            _log.info("pre-authorized tool=%s risk=%s", call.name, spec.risk.name)
        if needs_confirm and not pre_authorized:
            prompt = spec.confirm_prompt or self._format_prompt(
                spec.name, spec.risk, call.arguments, network=spec.network
            )
            kind = self._confirm_kind(spec)
            granted = False
            if spec.network:
                # Off-device send: always confirm per call, never remembered (privacy).
                decision = "once" if self._confirmer.confirm(prompt, kind) else ""
            else:
                key = self._grant_key(call)
                if key in self._session_grants:
                    granted = True
                    decision = "once"  # already approved this session — skip the card
                else:
                    decision = self._confirm_action(prompt, kind)
                    if decision == "session":
                        self._session_grants.add(key)
            if not decision:
                # Timeout (no answer) reads differently from a deliberate "no": say we
                # cancelled for lack of confirmation, not that the user declined.
                timed_out = bool(getattr(self._confirmer, "timed_out", False))
                reason = "timeout" if timed_out else "user_declined"
                _log.info("denied tool=%s risk=%s reason=%s", call.name, spec.risk.name, reason)
                self._audit.log(
                    tool=call.name,
                    arguments=call.arguments,
                    risk=spec.risk.name,
                    decision=Decision.DENIED,
                    ok=None,
                    detail="timed out without confirmation" if timed_out else "declined by user",
                )
                # Tell the model plainly what happened — acknowledge briefly and do NOT
                # re-ask or retry (that caused a nagging loop).
                if timed_out:
                    content = (
                        "No confirmation was received in time, so the action was "
                        "cancelled and NOT performed. Tell the user, in one short "
                        "sentence, that you cancelled it because you didn't get a "
                        "confirmation. Do not ask again or retry."
                    )
                else:
                    content = (
                        "The user declined this action, so it was not performed. "
                        "Acknowledge in one short sentence; do not ask again or retry."
                    )
                return ToolResult(name=call.name, content=content, ok=False)
            if granted:
                _log.info("auto-approved tool=%s via session grant", call.name)

        result = self._registry.dispatch(call.name, call.arguments)
        # Learn from the outcome: a success means the permission is granted; a
        # permission-style failure means it's missing. Refines the cached state when
        # the native check couldn't determine it.
        if spec.requires and self._permission_status is not None:
            from autobot import permissions

            if result.ok:
                permissions.note_observed(spec.requires, True)
            elif "permission" in result.content.lower():
                permissions.note_observed(spec.requires, False)
        _log.info(
            "allowed tool=%s risk=%s ok=%s args=%s",
            call.name,
            spec.risk.name,
            result.ok,
            call.arguments,
        )
        self._audit.log(
            tool=call.name,
            arguments=call.arguments,
            risk=spec.risk.name,
            decision=Decision.ALLOWED,
            ok=result.ok,
            detail=result.content,
        )
        return result

    @staticmethod
    def _confirm_kind(spec: ToolSpec) -> str:
        """Card tone for a confirmation: egress > destructive > write.

        ``"network"`` tints the card for an off-device send (the orange "data path"
        card); otherwise ``"danger"`` for a destructive action and ``"write"`` for a
        reversible change. Lets the UI make the off-device moment unmistakable.
        """
        if spec.network:
            return "network"
        if spec.risk >= Risk.DESTRUCTIVE:
            return "danger"
        return "write"

    @staticmethod
    def _format_prompt(
        name: str, risk: Risk, arguments: dict[str, object], *, network: bool = False
    ) -> str:
        """Build a clear, human-readable confirmation prompt for a risky call.

        Plain language about *what* will happen (with the actual target), so the
        person can decide — never the tool name, risk enum, or a raw args dict.
        Emoji are decorative on screen and stripped before the prompt is spoken.
        """
        if name == "delete_file":
            target = str(arguments.get("path", "this file"))
            return f"🗑️ Delete “{target}”? This permanently removes it — it can't be undone."
        if name == "uninstall_app":
            target = str(arguments.get("name", "this app"))
            return f"🗑️ Uninstall {target}? It'll be moved to the Trash."
        if name == "move_file":
            src = str(arguments.get("source", "a file"))
            dst = str(arguments.get("destination", "a new location"))
            return f"📦 Move “{src}” to “{dst}”?"
        if name == "run_command":
            command = str(arguments.get("command", "")).strip()
            cwd = str(arguments.get("cwd", "")).strip()
            where = f"\n\nin {cwd}" if cwd and cwd != "." else ""
            return f"Run this command?\n\n  $ {command}{where}"
        # MCP tool: name the server and the action plainly, and make the off-device moment
        # explicit for network-egress servers.
        ns = split_namespaced(name)
        if ns is not None:
            server, bare = ns
            detail = ", ".join(f"{k}: {v}" for k, v in arguments.items())
            suffix = f"\n\n  {detail}" if detail else ""
            disclosure = "\n\nThis sends data off-device (network server)." if network else ""
            return f"Run {server}: {bare.replace('_', ' ')}?{suffix}{disclosure}"
        # Generic fallback — readable, with the targets spelled out.
        detail = ", ".join(f"{k}: {v}" for k, v in arguments.items())
        suffix = f" ({detail})" if detail else ""
        verb = "permanently change things on" if risk is Risk.DESTRUCTIVE else "make changes to"
        return f"⚠️ This will {verb} your Mac{suffix}. Want me to go ahead?"
