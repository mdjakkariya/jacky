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

from typing import Protocol, runtime_checkable

from autobot.core.types import Decision, Risk, ToolCall, ToolResult
from autobot.logging_setup import get_logger
from autobot.tools.audit import AuditLog
from autobot.tools.registry import ToolRegistry

_log = get_logger("gate")


@runtime_checkable
class Confirmer(Protocol):
    """Asks the user to approve a risky action and returns their answer."""

    def confirm(self, prompt: str) -> bool:
        """Return ``True`` to proceed, ``False`` to cancel."""
        ...


class TerminalConfirmer:
    """Confirms via a ``[y/N]`` prompt on the terminal (default: No)."""

    def confirm(self, prompt: str) -> bool:
        """Ask on stdin; only an explicit ``y``/``yes`` proceeds."""
        answer = input(f"{prompt} [y/N] ").strip().lower()
        return answer in {"y", "yes"}


class AlwaysAllow:
    """A confirmer that approves everything (tests / non-interactive use)."""

    def confirm(self, prompt: str) -> bool:  # noqa: D102 - see class docstring
        return True


class AlwaysDeny:
    """A confirmer that rejects everything (tests)."""

    def confirm(self, prompt: str) -> bool:  # noqa: D102 - see class docstring
        return False


class PermissionGate:
    """Guards tool execution with risk classification, confirmation, and audit."""

    def __init__(
        self,
        registry: ToolRegistry,
        audit: AuditLog,
        confirmer: Confirmer,
        confirm_at_or_above: Risk = Risk.DESTRUCTIVE,
    ) -> None:
        self._registry = registry
        self._audit = audit
        self._confirmer = confirmer
        self._threshold = confirm_at_or_above

    def risk_of(self, name: str) -> Risk | None:
        """The risk level of a registered tool, or ``None`` if it's unknown.

        Lets callers (e.g. the orchestrator's spoken acknowledgement) tailor
        behavior to whether a tool merely reads or actually acts.
        """
        spec = self._registry.get(name)
        return spec.risk if spec is not None else None

    def execute(self, call: ToolCall) -> ToolResult:
        """Run one tool call through the gate; see the module docstring for policy."""
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

        if spec.risk >= self._threshold:
            prompt = spec.confirm_prompt or self._format_prompt(
                spec.name, spec.risk, call.arguments
            )
            if not self._confirmer.confirm(prompt):
                _log.info("denied tool=%s risk=%s reason=user_declined", call.name, spec.risk.name)
                self._audit.log(
                    tool=call.name,
                    arguments=call.arguments,
                    risk=spec.risk.name,
                    decision=Decision.DENIED,
                    ok=None,
                    detail="declined by user",
                )
                # Tell the model plainly the user said no — so it acknowledges briefly
                # and does NOT re-ask or retry (that caused a nagging loop).
                return ToolResult(
                    name=call.name,
                    content=(
                        "The user declined this action, so it was not performed. "
                        "Acknowledge in one short sentence; do not ask again or retry."
                    ),
                    ok=False,
                )

        result = self._registry.dispatch(call.name, call.arguments)
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
    def _format_prompt(name: str, risk: Risk, arguments: dict[str, object]) -> str:
        """Build the human-readable confirmation prompt for a risky call."""
        return f"⚠ '{name}' ({risk.name}) will run with {arguments}. Proceed?"
