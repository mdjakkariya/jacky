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


def test_assistant_profile_budget_unchanged() -> None:
    s = Settings(profile="assistant", llm_max_tokens=120, anthropic_max_tokens=512)
    out = _apply_profile_overrides(s)
    assert out.llm_max_tokens == 120
    assert out.anthropic_max_tokens == 512
