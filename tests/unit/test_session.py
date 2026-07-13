from __future__ import annotations

from autobot.agent.session import Session, TurnUsage


def test_session_defaults() -> None:
    s = Session(id="s1", cwd="/tmp/proj", model="gpt-x")
    assert s.history == []
    assert s.summary == ""
    assert s.delivery_mode == "voice"
    assert s.last_usage is None
    assert s.cost.in_tokens == 0 and s.cost.usd == 0.0 and s.cost.priced is False


def test_session_history_is_mutable_and_independent() -> None:
    a = Session(id="a", cwd="/x", model="m")
    b = Session(id="b", cwd="/y", model="m")
    a.history.append({"role": "user", "content": "hi"})
    assert a.history and not b.history  # no shared default list


def test_turn_usage_accumulates() -> None:
    u = TurnUsage()
    u.in_tokens += 10
    u.out_tokens += 3
    u.usd += 0.01
    u.priced = True
    assert (u.in_tokens, u.out_tokens, round(u.usd, 2), u.priced) == (10, 3, 0.01, True)
