"""Scenario value objects + validation."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte")

from autobot.e2e.scenario import ApprovePlan, FileExists, Scenario, Send


def _valid(**over: object) -> Scenario:
    base: dict[str, object] = {
        "name": "s",
        "autonomy": "auto",
        "strategy": "unattended",
        "task": "do X",
        "success_criteria": "did X",
    }
    base.update(over)
    return Scenario(**base)  # type: ignore[arg-type]


def test_valid_scenario_passes_validation() -> None:
    _valid().validate()  # no raise


def test_bad_autonomy_rejected() -> None:
    with pytest.raises(ValueError, match="autonomy"):
        _valid(autonomy="yolo").validate()


def test_scripted_requires_steps() -> None:
    with pytest.raises(ValueError, match="steps"):
        _valid(strategy="scripted", steps=()).validate()


def test_actions_and_checks_construct() -> None:
    s = _valid(strategy="scripted", steps=(Send("hi"), ApprovePlan()), checks=(FileExists("a.py"),))
    s.validate()
    assert isinstance(s.steps[0], Send) and s.steps[0].text == "hi"
    assert isinstance(s.checks[0], FileExists) and s.checks[0].path == "a.py"
