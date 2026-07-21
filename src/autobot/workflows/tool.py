"""The ``run_workflow`` tool: a deterministic, gated recipe runner.

``run_workflow`` looks up a :class:`~autobot.workflows.spec.WorkflowSpec` by name and
plays its steps in order, substituting ``{var}`` templates from the workflow's declared
inputs and each step's ``save_as`` output. It performs no side effects itself — every
step is dispatched through the turn's executor
(:data:`autobot.core.streaming.current_executor`), the same callback the orchestrator
wires to the permission gate for ordinary tool calls. So each step is gated at its OWN
risk: a destructive step still confirms, a network step still discloses. That per-step
enforcement is why the tool is registered ``Risk.READ_ONLY`` at the gate — a single
up-front confirmation of the whole recipe would be both weaker (masking the riskiest
step behind a generic prompt) and misleading (implying steps below the confirmed risk
are also being individually vetted, when a blanket confirm cannot express that). A
future phase may add an optional "confirm the recipe once, at its max step risk" mode;
v1's per-step gating is strictly safer and is deliberately the only behavior for now.
"""

from __future__ import annotations

import re
from typing import Any

from autobot.core.streaming import current_executor
from autobot.core.types import ErrorCategory, Risk, ToolCall
from autobot.logging_setup import get_logger
from autobot.tools.registry import ToolFailure, ToolRegistry, ToolSpec
from autobot.workflows.registry import WorkflowRegistry

_log = get_logger("workflow")

_VAR_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")
_FALSY = {"", "false", "0", "no"}


class _UnknownVariableError(Exception):
    """Raised internally when a ``{var}`` template references an undefined variable."""

    def __init__(self, var: str) -> None:
        """Store the offending variable name for the caller to report."""
        super().__init__(var)
        self.var = var


def _subst(value: Any, context: dict[str, str]) -> Any:
    r"""Recursively substitute ``{var}`` placeholders from ``context``.

    Walks dicts and lists depth-first; every string is scanned for ``{var}``
    references (``re.findall(r"\{([a-zA-Z0-9_]+)\}", s)``) and each is replaced with
    ``context[var]``. Non-string, non-container values (numbers, bools, ``None``) pass
    through unchanged.

    Args:
        value: The step argument (or ``when`` string) to resolve; may be a dict, list,
            str, or scalar.
        context: The workflow's current variable bindings (declared inputs plus any
            prior steps' ``save_as`` results), all as strings.

    Returns:
        The same shape as ``value`` with every ``{var}`` reference resolved.

    Raises:
        _UnknownVariableError: If a referenced variable is not a key in ``context``.
    """
    if isinstance(value, str):
        missing = [v for v in _VAR_RE.findall(value) if v not in context]
        if missing:
            raise _UnknownVariableError(missing[0])
        return _VAR_RE.sub(lambda m: context[m.group(1)], value)
    if isinstance(value, dict):
        return {k: _subst(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, context) for v in value]
    return value


def register_workflow_tools(registry: ToolRegistry, workflows: WorkflowRegistry) -> None:
    """Register the ``run_workflow`` tool, bound to ``workflows``.

    Args:
        registry: The tool registry to register ``run_workflow`` on.
        workflows: The catalog of discovered workflows to run by name.
    """

    def _run_workflow(name: str, args: dict[str, Any] | None = None) -> str:
        wf = workflows.get(name)
        if wf is None:
            return ToolFailure(f"unknown workflow: {name!r}", ErrorCategory.NOT_FOUND)

        executor = current_executor.get()
        if executor is None:
            return ToolFailure("run_workflow can only run inside an active turn")

        call_args = args or {}
        if not isinstance(call_args, dict):
            return ToolFailure(
                f"'args' must be an object, got {type(call_args).__name__}",
                ErrorCategory.INVALID,
            )
        missing_inputs = [k for k in wf.required_inputs if k not in call_args]
        if missing_inputs:
            return ToolFailure(
                f"workflow {name!r} missing required input(s): {', '.join(missing_inputs)}",
                ErrorCategory.INVALID,
            )

        context: dict[str, str] = {k: str(v) for k, v in call_args.items()}
        _log.info("workflow run name=%r steps=%d", name, len(wf.steps))

        lines: list[str] = []
        ran = 0
        skipped = 0
        for i, step in enumerate(wf.steps, start=1):
            if step.when is not None:
                try:
                    resolved_when = _subst(step.when, context)
                except _UnknownVariableError as exc:
                    return ToolFailure(
                        f"step {i} ({step.tool}) references unknown variable "
                        f"{{{exc.var}}} in its 'when' condition",
                        ErrorCategory.INVALID,
                    )
                if resolved_when.strip().lower() in _FALSY:
                    skipped += 1
                    lines.append(f"step {i} ({step.tool}): skipped (when={resolved_when!r})")
                    continue

            try:
                substituted_args = _subst(step.args, context)
            except _UnknownVariableError as exc:
                return ToolFailure(
                    f"step {i} ({step.tool}) references unknown variable {{{exc.var}}}",
                    ErrorCategory.INVALID,
                )

            # The gate: every step runs through the turn's executor, never
            # registry.dispatch directly, so it is classified and confirmed at its
            # own risk (see module docstring).
            result = executor(ToolCall(step.tool, substituted_args))
            if not result.ok:
                _log.info("workflow run name=%r stopped step=%d tool=%r", name, i, step.tool)
                return ToolFailure(
                    f"workflow {name!r} stopped at step {i} ({step.tool}): {result.content}",
                    result.category,
                )

            if step.save_as:
                context[step.save_as] = result.content
            ran += 1
            lines.append(f"step {i} ({step.tool}): ok")

        _log.info("workflow run name=%r done ran=%d skipped=%d", name, ran, skipped)
        summary = (
            f"Ran workflow {name!r}: {len(wf.steps)} step(s) ({ran} executed, {skipped} skipped)."
        )
        return "\n".join([summary, *lines])

    registry.register(
        ToolSpec(
            name="run_workflow",
            description=(
                "Run a saved deterministic workflow (recipe) by exact name. Its steps "
                "execute in order and each is individually gated by the permission "
                "system at that step's own risk — a destructive step still confirms, a "
                "network step still discloses; run_workflow itself performs no side "
                "effects. Pass any of the workflow's declared inputs via `args` "
                '(e.g. {"since_tag": "v1.0"}).'
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact workflow name."},
                    "args": {
                        "type": "object",
                        "description": "Input values declared by the workflow, keyed by name.",
                    },
                },
                "required": ["name"],
            },
            handler=_run_workflow,
            risk=Risk.READ_ONLY,
            core=False,
        )
    )
    _log.info("workflow tools registered")
