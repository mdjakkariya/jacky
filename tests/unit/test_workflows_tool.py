"""``run_workflow`` tool: input resolution, templating, ``when``, ``save_as``, gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.core.streaming import current_executor
from autobot.core.types import ErrorCategory, Risk, ToolCall, ToolResult
from autobot.tools.registry import ToolRegistry
from autobot.workflows.registry import WorkflowDir, WorkflowRegistry
from autobot.workflows.tool import register_workflow_tools


def _write_workflow(root: Path, name: str, steps_yaml: str, *, required_seed: bool = True) -> None:
    """Write a minimal WORKFLOW.md under ``root/name`` with the given steps YAML."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    inputs = "inputs:\n  - name: seed\n    required: true\n" if required_seed else ""
    (d / "WORKFLOW.md").write_text(
        f"---\nname: {name}\ndescription: a test workflow\n{inputs}---\n\n"
        f"```yaml\n{steps_yaml}\n```\n",
        encoding="utf-8",
    )


THREADING_STEPS = """
steps:
  - tool: noop
    args: { x: "{seed}" }
    save_as: out
  - tool: noop2
    args: { y: "{out}" }
    when: "{out}"
"""

FAIL_HALT_STEPS = """
steps:
  - tool: step_a
    args: {}
    save_as: a
  - tool: step_b
    args: {}
  - tool: step_c
    args: {}
"""

SKIP_STEPS = """
steps:
  - tool: maybe
    args: { y: "1" }
    when: "{flag}"
"""

GHOST_STEPS = """
steps:
  - tool: noop
    args: { x: "{ghost}" }
"""


@pytest.fixture
def wf_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def wf_registry(wf_root: Path) -> WorkflowRegistry:
    _write_workflow(wf_root, "demo", THREADING_STEPS)
    _write_workflow(wf_root, "demo-fail", FAIL_HALT_STEPS, required_seed=False)
    _write_workflow(wf_root, "demo-skip", SKIP_STEPS, required_seed=False)
    _write_workflow(wf_root, "demo-ghost", GHOST_STEPS, required_seed=False)
    return WorkflowRegistry([WorkflowDir(wf_root, "user", 20)])


@pytest.fixture
def registry(wf_registry: WorkflowRegistry) -> ToolRegistry:
    reg = ToolRegistry()
    register_workflow_tools(reg, wf_registry)
    return reg


class FakeExecutor:
    """Records every :class:`ToolCall` it receives and returns canned results."""

    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self.calls: list[ToolCall] = []
        self._results = results or {}

    def __call__(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        canned = self._results.get(call.name)
        if canned is not None:
            return canned
        return ToolResult(name=call.name, content=f"{call.name}-result", ok=True)


def _run_with_executor(
    registry: ToolRegistry, fake: FakeExecutor | None, name: str, args: dict[str, object] | None
) -> ToolResult:
    token = current_executor.set(fake)
    try:
        return registry.dispatch("run_workflow", {"name": name, "args": args})
    finally:
        current_executor.reset(token)


def test_run_workflow_registered_read_only(registry: ToolRegistry) -> None:
    spec = registry.get("run_workflow")
    assert spec is not None
    assert spec.risk == Risk.READ_ONLY


def test_threads_save_as_into_next_step_and_substitutes_inputs(registry: ToolRegistry) -> None:
    fake = FakeExecutor()
    result = _run_with_executor(registry, fake, "demo", {"seed": "S"})
    assert result.ok, result.content
    assert len(fake.calls) == 2
    assert fake.calls[0] == ToolCall("noop", {"x": "S"})
    # step 2's {out} was substituted with step 1's result content.
    assert fake.calls[1] == ToolCall("noop2", {"y": "noop-result"})


def test_failing_step_stops_workflow_and_names_it(registry: ToolRegistry) -> None:
    fake = FakeExecutor(
        results={"step_b": ToolResult(name="step_b", content="boom", ok=False, category="denied")}
    )
    result = _run_with_executor(registry, fake, "demo-fail", {})
    assert result.ok is False
    assert "step 2" in result.content
    assert "step_b" in result.content
    # step_c must never have run.
    assert [c.name for c in fake.calls] == ["step_a", "step_b"]


def test_unknown_workflow_is_not_found(registry: ToolRegistry) -> None:
    fake = FakeExecutor()
    result = _run_with_executor(registry, fake, "nope", {})
    assert result.ok is False
    assert result.category == ErrorCategory.NOT_FOUND


def test_missing_required_input_is_invalid(registry: ToolRegistry) -> None:
    fake = FakeExecutor()
    result = _run_with_executor(registry, fake, "demo", {})
    assert result.ok is False
    assert result.category == ErrorCategory.INVALID
    assert "seed" in result.content


def test_no_executor_fails_with_active_turn_message(registry: ToolRegistry) -> None:
    assert current_executor.get() is None
    result = registry.dispatch("run_workflow", {"name": "demo", "args": {"seed": "S"}})
    assert result.ok is False
    assert "active turn" in result.content


@pytest.mark.parametrize("flag_value", ["", "false", "FALSE", "0", "no"])
def test_when_step_skipped_when_falsy(registry: ToolRegistry, flag_value: str) -> None:
    fake = FakeExecutor()
    result = _run_with_executor(registry, fake, "demo-skip", {"flag": flag_value})
    assert result.ok, result.content
    assert fake.calls == []  # the sole step was skipped, never dispatched


def test_when_step_runs_when_truthy(registry: ToolRegistry) -> None:
    fake = FakeExecutor()
    result = _run_with_executor(registry, fake, "demo-skip", {"flag": "yes"})
    assert result.ok, result.content
    assert len(fake.calls) == 1


def test_unknown_var_in_step_args_is_invalid_and_stops(registry: ToolRegistry) -> None:
    fake = FakeExecutor()
    result = _run_with_executor(registry, fake, "demo-ghost", {})
    assert result.ok is False
    assert result.category == ErrorCategory.INVALID
    assert "ghost" in result.content
    assert fake.calls == []


def test_non_dict_args_is_invalid(registry: ToolRegistry) -> None:
    """Passing a non-dict for 'args' should return INVALID, not raise."""
    fake = FakeExecutor()
    result = _run_with_executor(registry, fake, "demo", ["oops"])  # type: ignore[arg-type]
    assert result.ok is False
    assert result.category == ErrorCategory.INVALID
    assert "must be an object" in result.content
    assert "list" in result.content
    assert fake.calls == []


def test_when_references_undefined_var_is_invalid(wf_root: Path, registry: ToolRegistry) -> None:
    """A step's 'when' condition referencing an undefined variable should stop workflow."""
    _write_workflow(
        wf_root,
        "demo-when-ghost",
        """
steps:
  - tool: noop
    args: { x: "1" }
    when: "{undefined_var}"
""",
        required_seed=False,
    )
    fake = FakeExecutor()
    result = _run_with_executor(registry, fake, "demo-when-ghost", {})
    assert result.ok is False
    assert result.category == ErrorCategory.INVALID
    assert "undefined_var" in result.content
    assert "when" in result.content
    assert fake.calls == []
