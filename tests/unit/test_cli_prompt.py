"""Choice parsers (pure) and the / + @ completer."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.cli import prompt


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", "approve"),
        ("y", "approve"),
        ("approve", "approve"),
        ("2", "refine"),
        ("e", "refine"),
        ("3", "reject"),
        ("n", "reject"),
        ("no", "reject"),
    ],
)
def test_parse_plan_choice(raw: str, expected: str) -> None:
    ans = prompt.parse_plan_choice(raw)
    assert ans is not None and ans.value == expected


def test_parse_plan_choice_unrecognized_is_none() -> None:
    assert prompt.parse_plan_choice("what?") is None


@pytest.mark.parametrize(
    "raw,expected",
    [("1", "yes"), ("y", "yes"), ("2", "no"), ("n", "no")],
)
def test_parse_confirm_choice(raw: str, expected: str) -> None:
    ans = prompt.parse_confirm_choice(raw)
    assert ans is not None and ans.value == expected


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


@pytest.mark.parametrize(
    "raw,expected",
    [("go ahead", "yes"), ("sure", "yes"), ("nope", "no"), ("cancel", "no")],
)
def test_parse_confirm_free_text_intent(raw: str, expected: str) -> None:
    ans = prompt.parse_confirm_choice(raw)
    assert ans is not None and ans.value == expected


def test_parse_confirm_genuinely_ambiguous_is_none() -> None:
    assert prompt.parse_confirm_choice("why?") is None


def test_parse_plan_free_text_intent_keeps_edit_explicit() -> None:
    approve = prompt.parse_plan_choice("go ahead")
    reject = prompt.parse_plan_choice("no thanks")
    refine = prompt.parse_plan_choice("e")
    assert approve is not None and approve.value == "approve"
    assert reject is not None and reject.value == "reject"
    assert refine is not None and refine.value == "refine"  # edit stays explicit
