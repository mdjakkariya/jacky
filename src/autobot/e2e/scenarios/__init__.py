"""The seed scenario corpus — a growing set of real-world tasks. Add one per bug report."""

from __future__ import annotations

from autobot.e2e.scenario import (
    ApprovePlan,
    Confirm,
    Expect,
    FileContains,
    FileExists,
    Scenario,
    ScreenContains,
    Send,
)

ALL: tuple[Scenario, ...] = (
    Scenario(
        name="create-file",
        autonomy="plan",
        strategy="scripted",
        task="create hello.py that prints hi",
        success_criteria="Created hello.py that prints a greeting, showed a diff, replied clearly.",
        steps=(
            Send("create hello.py that prints hi"),
            Expect("plan_card", 90.0),
            ApprovePlan(),
            Expect("idle_prompt", 120.0),
        ),
        checks=(FileExists("hello.py"), FileContains("hello.py", "print")),
    ),
    Scenario(
        name="run-command",
        autonomy="confirm",
        strategy="scripted",
        task="run the command: echo e2e-ok",
        success_criteria="Asked before running the command, then ran it and reported the output.",
        steps=(
            Send("run this shell command: echo e2e-ok"),
            Expect("permission_card", 90.0),
            Confirm(),
            Expect("idle_prompt", 120.0),
        ),
        checks=(ScreenContains("e2e-ok"),),
    ),
    Scenario(
        name="edit-file",
        autonomy="auto",
        strategy="unattended",
        task="add a module docstring to foo.py",
        success_criteria="Added a docstring to foo.py; the diff shows the change.",
        seed_files={"foo.py": "def foo():\n    return 1\n"},
        checks=(FileContains("foo.py", '"""'),),
    ),
    Scenario(
        name="build-small-thing",
        autonomy="auto",
        strategy="unattended",
        task="create calc.py with add(a, b) and a test test_calc.py, then run the test",
        success_criteria="Created calc.py and a passing test; the run reported the test passing.",
        checks=(FileExists("calc.py"), FileExists("test_calc.py")),
    ),
    Scenario(
        name="slash-and-chat",
        autonomy="auto",
        strategy="scripted",
        task="edit foo.py then use slash commands",
        success_criteria="/diff showed the change, /undo reverted it, and the greeting got a "
        "direct reply with no plan card.",
        seed_files={"foo.py": "x = 1\n"},
        steps=(
            Send("append a comment '# touched' to foo.py"),
            Expect("idle_prompt", 120.0),
            Send("/diff"),
            Expect("idle_prompt", 30.0),
            Send("/undo"),
            Expect("idle_prompt", 30.0),
            Send("hi, what can you do?"),
            Expect("reply_present", 60.0),
            Expect("idle_prompt", 60.0),
        ),
        checks=(FileContains("foo.py", "x = 1"),),
    ),
)


def all_scenarios() -> tuple[Scenario, ...]:
    """All seed scenarios."""
    return ALL


def by_name(name: str) -> Scenario:
    """The scenario named ``name`` (raises ``KeyError`` if unknown)."""
    for s in ALL:
        if s.name == name:
            return s
    raise KeyError(name)
