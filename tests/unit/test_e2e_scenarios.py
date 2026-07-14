"""The seed scenario corpus is well-formed."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte")

from autobot.e2e import scenarios


def test_seed_scenarios_valid() -> None:
    names = {s.name for s in scenarios.all_scenarios()}
    assert {
        "create-file",
        "run-command",
        "readonly-command-no-prompt",
        "confirm-freetext-yes",
        "edit-file",
        "build-small-thing",
        "undo-edit",
        "slash-and-chat",
    } <= names
    for s in scenarios.all_scenarios():
        s.validate()  # no raise


def test_by_name_and_unknown() -> None:
    assert scenarios.by_name("create-file").autonomy == "plan"
    with pytest.raises(KeyError):
        scenarios.by_name("nope")


def test_modes_are_covered() -> None:
    autonomies = {s.autonomy for s in scenarios.all_scenarios()}
    assert {"plan", "confirm", "auto"} <= autonomies
