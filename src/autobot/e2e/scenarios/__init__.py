"""The seed scenario corpus — a growing set of real-world tasks. Add one per bug report."""

from __future__ import annotations

from autobot.e2e.scenario import (
    ApprovePlan,
    Confirm,
    Expect,
    FileContains,
    FileExists,
    FileLacks,
    Scenario,
    ScreenContains,
    Send,
)

# Real LLM turns (esp. local models doing read-only exploration + planning) can take a
# couple of minutes; a slash command (/diff, /undo — daemon-backed, no LLM) is near-instant.
_TURN = 240.0  # wait for a plan/permission card or a turn to reach idle
_CMD = 30.0  # wait for a daemon-backed command to return to idle

ALL: tuple[Scenario, ...] = (
    Scenario(
        name="create-file",
        autonomy="plan",
        strategy="scripted",
        task="create hello.py that prints hi",
        success_criteria="Created hello.py that prints a greeting, showed a diff, replied clearly.",
        steps=(
            Send("create hello.py that prints hi"),
            Expect("plan_card", _TURN),
            ApprovePlan(),
            Expect("idle_prompt", _TURN),
        ),
        checks=(FileExists("hello.py"), FileContains("hello.py", "print")),
    ),
    Scenario(
        name="run-command",
        autonomy="confirm",
        strategy="scripted",
        task="run this shell command to make a folder: mkdir e2e_dir",
        success_criteria="Showed a clear 'Run this command?' prompt with the actual command, "
        "then ran it after approval and created the folder.",
        steps=(
            Send("run this shell command to make a folder: mkdir e2e_dir"),
            Expect("permission_card", _TURN),  # a mutating command DOES prompt
            Confirm(),  # approve with the y key (the real affordance)
            Expect("idle_prompt", _TURN),
        ),
        checks=(FileExists("e2e_dir"),),
    ),
    Scenario(
        name="readonly-command-no-prompt",
        autonomy="confirm",
        strategy="scripted",
        task="run this shell command: echo e2e-readonly-ok",
        success_criteria="Ran the read-only echo command and showed its output WITHOUT asking "
        "for confirmation (no permission card).",
        steps=(
            Send("run this shell command: echo e2e-readonly-ok"),
            # A read-only command auto-runs: the turn goes straight to idle with no gate. If a
            # permission card wrongly blocked it, the turn would park at the '>' answer prompt
            # and idle_prompt would never hold — so this Expect deterministically asserts that
            # no confirmation was requested.
            Expect("idle_prompt", _TURN),
        ),
        checks=(ScreenContains("e2e-readonly-ok"),),
    ),
    Scenario(
        name="confirm-freetext-yes",
        autonomy="confirm",
        strategy="scripted",
        task="run this shell command to make a folder: mkdir e2e_freetext",
        success_criteria="Asked before the command, accepted the natural-language 'go ahead' "
        "as a yes, then ran it and created the folder.",
        steps=(
            Send("run this shell command to make a folder: mkdir e2e_freetext"),
            Expect("permission_card", _TURN),
            Send("go ahead"),  # free-text intent (not 1/y) must resolve to yes
            Expect("idle_prompt", _TURN),
        ),
        checks=(FileExists("e2e_freetext"),),
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
        name="undo-edit",
        autonomy="auto",
        strategy="scripted",
        task="edit a file then undo it",
        success_criteria="Edited foo.py to add a marker line, then /undo restored the "
        "original content (the marker is gone).",
        seed_files={"foo.py": "ORIGINAL = 1\n"},
        steps=(
            Send("add a line '# marker' to the end of foo.py"),
            Expect("idle_prompt", _TURN),
            Send("/undo"),
            Expect("idle_prompt", _CMD),
        ),
        # After undo the file must be back to its seed: still has ORIGINAL, marker removed.
        checks=(FileContains("foo.py", "ORIGINAL"), FileLacks("foo.py", "# marker")),
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
            Expect("idle_prompt", _TURN),
            Send("/diff"),
            Expect("idle_prompt", _CMD),
            Send("/undo"),
            Expect("idle_prompt", _CMD),
            Send("hi, what can you do?"),
            Expect("reply_present", _TURN),
            Expect("idle_prompt", _TURN),
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
