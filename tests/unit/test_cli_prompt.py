"""Choice parsers (pure) and the / + @ completer."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.cli import prompt


def test_slash_completer_offers_matching_commands() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    comp = prompt.JackCompleter({"/help": "h", "/clear": "c", "/exit": "e"}, cwd=".")
    doc = Document("/he", cursor_position=3)
    texts = [c.text for c in comp.get_completions(doc, None)]
    assert "/help" in texts and "/clear" not in texts


def test_file_completer_offers_cwd_paths(tmp_path: Path) -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    (tmp_path / "apples.py").write_text("x", encoding="utf-8")
    (tmp_path / "bananas.py").write_text("x", encoding="utf-8")
    comp = prompt.JackCompleter({}, cwd=str(tmp_path))
    doc = Document("fix @ap", cursor_position=7)
    texts = [c.text for c in comp.get_completions(doc, None)]
    assert any("apples.py" in t for t in texts)
    assert not any("bananas.py" in t for t in texts)


def _run_choice(keys: str, options: list[tuple[str, str, str]]) -> str:
    """Drive read_choice headlessly by piping keystrokes through a fake terminal."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from autobot.cli.prompt import read_choice

    with create_pipe_input() as inp:
        inp.send_text(keys)
        with create_app_session(input=inp, output=DummyOutput()):
            return read_choice("Run this command?\n\n  $ mkdir x", options)


def test_read_choice_single_key_yes() -> None:
    opts = [("y", "yes", "yes"), ("n", "no", "no")]
    assert _run_choice("y", opts) == "yes"


def test_read_choice_single_key_no() -> None:
    opts = [("y", "yes", "yes"), ("n", "no", "no")]
    assert _run_choice("n", opts) == "no"


def test_read_choice_ignores_unrecognized_key_then_resolves() -> None:
    # A stray key is ignored (keeps waiting); the next valid key resolves it.
    opts = [("y", "yes", "yes"), ("n", "no", "no")]
    assert _run_choice("qz y", opts) == "yes"


def test_read_choice_plan_options() -> None:
    opts = [("y", "approve", "approve"), ("e", "edit", "refine"), ("n", "no", "reject")]
    assert _run_choice("e", opts) == "refine"
