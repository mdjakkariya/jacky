"""Tests for the provider-agnostic price book (pure)."""

from __future__ import annotations

from datetime import datetime, timezone

from autobot.usage.pricing import price_usd

_BEFORE = datetime(2026, 7, 14, tzinfo=timezone.utc)  # within the Sonnet-5 intro window
_AFTER = datetime(2026, 9, 1, tzinfo=timezone.utc)  # after the intro window ends


def test_sonnet5_uses_intro_rate_before_the_cutoff() -> None:
    # Intro $2/$10 per MTok: 1M in + 1M out = $2 + $10 = $12.
    assert (
        price_usd(
            "anthropic", "claude-sonnet-5", in_tokens=1_000_000, out_tokens=1_000_000, at=_BEFORE
        )
        == 12.0
    )


def test_sonnet5_uses_standard_rate_after_the_cutoff() -> None:
    # Standard $3/$15: 1M + 1M = $18.
    assert (
        price_usd(
            "anthropic", "claude-sonnet-5", in_tokens=1_000_000, out_tokens=1_000_000, at=_AFTER
        )
        == 18.0
    )


def test_prefix_match_picks_point_release() -> None:
    # claude-opus-4-8 matches the "claude-opus-4" prefix at $5/$25.
    assert (
        price_usd("anthropic", "claude-opus-4-8", in_tokens=1_000_000, out_tokens=0, at=_AFTER)
        == 5.0
    )


def test_cache_read_and_write_priced_on_input_rate() -> None:
    # Haiku $1/$5. cache_write 1.25x, cache_read 0.1x of the $1 input rate.
    # 1M write = $1.25 ; 1M read = $0.10.
    cost = price_usd(
        "anthropic",
        "claude-haiku-4-5",
        in_tokens=0,
        out_tokens=0,
        cache_read=1_000_000,
        cache_write=1_000_000,
        at=_AFTER,
    )
    assert cost is not None and round(cost, 4) == 1.35


def test_local_ollama_is_a_real_zero_not_unknown() -> None:
    # Any local model is priced at $0 (priced, not None).
    assert price_usd("ollama", "qwen3:8b", in_tokens=999, out_tokens=999, at=_AFTER) == 0.0


def test_unknown_provider_or_model_returns_none() -> None:
    assert price_usd("openai", "gpt-nonexistent", in_tokens=10, out_tokens=10, at=_AFTER) is None
    assert price_usd("mystery", "x", in_tokens=10, out_tokens=10, at=_AFTER) is None
