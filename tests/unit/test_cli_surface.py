"""FakeSurface records commits/activity and answers ask() from a preset queue."""

from __future__ import annotations

import asyncio

from autobot.cli.classify import Segment
from autobot.cli.prompt import Answer
from tests.unit.support import FakeSurface


def test_fake_surface_records_commit_and_activity() -> None:
    s = FakeSurface()
    s.commit("line-1")
    s.set_activity("Reading")
    s.clear_activity()
    assert s.commits == ["line-1"]
    assert s.activity == ["Reading", ""]  # clear records an empty activity


def test_fake_surface_ask_returns_preset_answer() -> None:
    s = FakeSurface(answers=[Answer("approve")])
    got = asyncio.run(s.ask(Segment("plan", "the plan")))
    assert got == Answer("approve")
