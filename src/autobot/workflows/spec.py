"""Pure parsing and validation for ``WORKFLOW.md`` workflow definitions.

No filesystem I/O lives here so the whole module is trivially unit-testable: the
registry reads bytes off disk and hands the text in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from autobot.skills.spec import parse_frontmatter

_NAME_RE = re.compile(r"^[a-z0-9-]+$")
_NAME_MAX = 64
_FENCE_RE = re.compile(r"^```")


class WorkflowError(Exception):
    """A ``WORKFLOW.md`` is malformed or violates the workflow standard."""


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """A single tool invocation within a workflow.

    Attributes:
        tool: The name of the tool to invoke (required).
        args: Keyword arguments to pass to the tool (default: {}).
        when: Optional condition; the step executes only if this expression is truthy.
        save_as: Optional variable name; the step's result is stored under this name.
    """

    tool: str
    args: dict[str, Any]
    when: str | None
    save_as: str | None


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    """A parsed workflow definition.

    Attributes:
        name: The workflow name (lowercase, alphanumeric + hyphens, ≤64 chars).
        description: A human-readable description of what the workflow does.
        inputs: All input names (in order of appearance in frontmatter).
        required_inputs: Input names marked with ``required: true``.
        steps: Sequence of tool-call steps to execute in order.
        path: The file path where this workflow was defined.
    """

    name: str
    description: str
    inputs: tuple[str, ...]
    required_inputs: tuple[str, ...]
    steps: tuple[WorkflowStep, ...]
    path: Path


def parse_workflow(text: str, *, path: Path) -> WorkflowSpec:
    """Parse and validate a ``WORKFLOW.md`` file into a :class:`WorkflowSpec`.

    Args:
        text: The full ``WORKFLOW.md`` contents.
        path: The file's location, stored on the resulting spec.

    Returns:
        A validated :class:`WorkflowSpec`.

    Raises:
        WorkflowError: If parsing or validation fails (bad name/description,
            missing or malformed steps block, etc.).
    """
    # Split frontmatter and body
    meta, body = parse_frontmatter(text)

    # Validate name
    name = meta.get("name")
    if not isinstance(name, str) or not name:
        raise WorkflowError("name is required")
    if len(name) > _NAME_MAX:
        raise WorkflowError(f"name exceeds {_NAME_MAX} characters")
    if not _NAME_RE.fullmatch(name):
        raise WorkflowError("name must be lowercase letters, digits, and hyphens only")

    # Validate description
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise WorkflowError("description is required")

    # Parse inputs
    inputs_list = meta.get("inputs", [])
    if not isinstance(inputs_list, list):
        inputs_list = []
    input_names_list = []
    required_names_list = []
    for inp in inputs_list:
        if isinstance(inp, dict):
            inp_name = inp.get("name")
            if isinstance(inp_name, str):
                input_names_list.append(inp_name)
                if inp.get("required") is True:
                    required_names_list.append(inp_name)
    input_names = tuple(input_names_list)
    required_names = tuple(required_names_list)

    # Extract the first fenced code block from body
    steps_yaml = _extract_fenced_block(body)
    if steps_yaml is None:
        raise WorkflowError("no fenced code block (steps definition) found in body")

    # YAML load the steps block
    try:
        steps_data = yaml.safe_load(steps_yaml)
    except yaml.YAMLError as e:
        raise WorkflowError(f"invalid YAML in steps block: {e}") from e

    if not isinstance(steps_data, dict):
        raise WorkflowError("steps block must parse to a mapping (dict)")

    steps_list = steps_data.get("steps")
    if not isinstance(steps_list, list):
        raise WorkflowError("steps block must have a 'steps' list")

    # Build WorkflowStep objects
    steps = []
    for step_data in steps_list:
        if not isinstance(step_data, dict):
            raise WorkflowError("each step must be a mapping (dict)")

        tool = step_data.get("tool")
        if not isinstance(tool, str) or not tool:
            raise WorkflowError("each step must have a non-empty 'tool' string")

        args = step_data.get("args", {})
        if not isinstance(args, dict):
            args = {}

        when = step_data.get("when")
        if when is not None and not isinstance(when, str):
            when = None

        save_as = step_data.get("save_as")
        if save_as is not None and not isinstance(save_as, str):
            save_as = None

        steps.append(WorkflowStep(tool=tool, args=args, when=when, save_as=save_as))

    return WorkflowSpec(
        name=name,
        description=description,
        inputs=input_names,
        required_inputs=required_names,
        steps=tuple(steps),
        path=path,
    )


def _extract_fenced_block(text: str) -> str | None:
    """Extract the first fenced code block from text.

    Scans for a line starting with ``` (three backticks), optionally followed
    by a language specifier (e.g., `yaml`), and captures everything until the
    next line that is exactly ````` ``` ```.

    Args:
        text: The Markdown body text.

    Returns:
        The content inside the fence (without the fence markers), or None if
        no fenced block is found.
    """
    lines = text.split("\n")
    start_idx = None

    # Find opening fence
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            start_idx = i
            break

    if start_idx is None:
        return None

    # Find closing fence
    for i in range(start_idx + 1, len(lines)):
        if _FENCE_RE.match(lines[i]):
            # Return content between fences (excluding the fence lines)
            return "\n".join(lines[start_idx + 1 : i])

    # No closing fence found
    return None
