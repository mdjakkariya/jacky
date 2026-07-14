"""_apply_profile_overrides raises the output budget only for the coder profile."""

from __future__ import annotations

from autobot.app import _apply_profile_overrides
from autobot.config import Settings


def test_coder_profile_raises_output_budget() -> None:
    s = Settings(
        profile="coder",
        llm_max_tokens=120,
        anthropic_max_tokens=512,
        coder_llm_max_tokens=4096,
    )
    out = _apply_profile_overrides(s)
    assert out.llm_max_tokens == 4096  # local budget
    assert out.anthropic_max_tokens == 4096  # cloud budget — else cloud coder is capped at 512


def test_coder_profile_raises_tool_round_cap() -> None:
    # A real multi-step coder task needs far more than the assistant's 8 rounds; the coder
    # profile raises the cap so it doesn't stop early at "I hit my step limit".
    s = Settings(profile="coder", max_tool_rounds=8, coder_max_tool_rounds=50)
    assert _apply_profile_overrides(s).max_tool_rounds == 50


def test_assistant_profile_budget_and_rounds_unchanged() -> None:
    s = Settings(
        profile="assistant", llm_max_tokens=120, anthropic_max_tokens=512, max_tool_rounds=8
    )
    out = _apply_profile_overrides(s)
    assert out.llm_max_tokens == 120
    assert out.anthropic_max_tokens == 512
    assert out.max_tool_rounds == 8  # voice turns keep the small cap
