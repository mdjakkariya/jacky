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
        name="cost-summary",
        autonomy="auto",
        strategy="scripted",
        task="run a read-only command, then check /cost",
        success_criteria="After a turn recorded usage, /cost rendered a summary with per-window "
        "totals (Today / All time), a Cache r/w column, and a cost figure — no crash.",
        steps=(
            # A read-only echo auto-runs (no gate) and still records a usage row; then /cost
            # renders the summary from the (E2E-isolated) ledger via the daemon endpoint.
            Send("run this shell command: echo e2e-cost-ok"),
            Expect("idle_prompt", _TURN),
            Send("/cost"),
            Expect("cost_view", _CMD),
        ),
        checks=(ScreenContains("All time"),),
    ),
    Scenario(
        # Exercises the LSP-backed `rename_symbol` tool end-to-end (epic #105): a real language
        # server (pylsp, if installed) computes a cross-file WorkspaceEdit and the tool applies it
        # atomically. The seed puts the definition in one file and the call + import in another, so
        # a pass proves a *semantic* rename (all three sites) — not a textual s/greet/welcome/ that
        # would also mangle the "greeter" module name. If no Python server is on PATH the tool
        # declines (no unsafe textual rename); this scenario then legitimately won't pass, which is
        # the intended signal to install one, so it's driven only when a server is available.
        name="rename-symbol",
        autonomy="auto",
        strategy="unattended",
        task="rename the function greet to welcome everywhere it is used",
        success_criteria="Renamed the function greet to welcome across both files — the "
        "definition in greeter.py and the import + call site in main.py — with no leftover "
        "'greet' references.",
        seed_files={
            "greeter.py": 'def greet(name):\n    return f"Hello, {name}!"\n',
            "main.py": 'from greeter import greet\n\nprint(greet("world"))\n',
        },
        checks=(
            FileContains("greeter.py", "def welcome"),  # the definition was renamed
            FileLacks("greeter.py", "greet"),  # …and nothing greet-shaped is left behind
            FileContains("main.py", "welcome("),  # the call site was renamed (not just the import)
            FileLacks("main.py", "greet("),  # …and the old call is gone (greeter module name stays)
        ),
    ),
    Scenario(
        # A richer LSP exercise (epic #105): a cross-file rename that a *textual* rename would get
        # wrong. `scale` is a function in mathx.py used from a.py and b.py (three files, three call
        # sites incl. two in b.py) — AND there's a decoy module-level `scale` string in notes.py
        # that is a different symbol. A semantic (LSP) rename touches only the function's real
        # references and leaves the decoy alone; a naive s/scale/double/ would corrupt notes.py.
        # The "then check for problems" tail nudges the `diagnostics` tool in the same turn. This
        # is the effectiveness proof: semantic precision + multi-tool orchestration on a tiny seed.
        name="rename-across-files",
        autonomy="auto",
        strategy="unattended",
        task="rename the scale function from mathx.py to double everywhere it is used, then "
        "check the files have no problems",
        success_criteria="Renamed the mathx.scale function to double across mathx.py and both "
        "call sites (a.py, b.py) with a semantic rename — leaving the unrelated 'scale' string "
        "variable in notes.py untouched — and confirmed no problems remain.",
        seed_files={
            "mathx.py": "def scale(x):\n    return x * 2\n",
            "a.py": "from mathx import scale\n\n\ndef run_a():\n    return scale(10)\n",
            "b.py": "from mathx import scale\n\n\ndef run_b():\n    return scale(20) + scale(30)\n",
            "notes.py": 'scale = "map scale is 1:1000"\n\n\ndef describe():\n    return scale\n',
        },
        checks=(
            FileContains("mathx.py", "def double"),  # the definition was renamed
            FileContains("a.py", "double(10)"),  # call site in a.py renamed
            FileContains("b.py", "double(20)"),  # call sites in b.py renamed
            FileLacks("a.py", "scale"),  # import + call both gone ('mathx' has no 'scale')
            FileContains("notes.py", 'scale = "map scale is 1:1000"'),  # decoy untouched (semantic)
        ),
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
