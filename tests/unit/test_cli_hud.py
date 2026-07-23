"""Unit tests for the CLI HUD (state, segments, composition, config resolution)."""

from __future__ import annotations

from autobot.cli import hud
from autobot.config import Settings


def _settings(**over: object) -> Settings:
    from dataclasses import replace

    return replace(Settings(), **over)  # type: ignore[arg-type]


def test_defaults_enable_essential_preset() -> None:
    rows = hud.resolve_config(_settings())
    assert len(rows) == 1  # essential is a single row
    keys = [k for k, _ in rows[0]]
    assert keys == ["autonomy", "model", "context", "git", "cwd"]


def test_full_preset_is_two_rows() -> None:
    rows = hud.resolve_config(_settings(hud_preset="full"))
    assert len(rows) == 2
    assert [k for k, _ in rows[0]] == ["autonomy", "model", "git", "cwd"]
    assert [k for k, _ in rows[1]] == ["context", "tokens", "cost", "mcp", "skills", "elapsed"]


def test_disabled_returns_no_rows() -> None:
    assert hud.resolve_config(_settings(hud_enabled=False)) == []


def test_explicit_segments_override_preset_into_one_row() -> None:
    rows = hud.resolve_config(_settings(hud_segments=["autonomy", "context"]))
    assert len(rows) == 1
    assert [k for k, _ in rows[0]] == ["autonomy", "context"]


def test_unknown_segment_key_is_dropped() -> None:
    rows = hud.resolve_config(_settings(hud_segments=["autonomy", "bogus", "cwd"]))
    assert [k for k, _ in rows[0]] == ["autonomy", "cwd"]


def test_unknown_preset_falls_back_to_essential() -> None:
    rows = hud.resolve_config(_settings(hud_preset="nope"))
    assert [k for k, _ in rows[0]] == ["autonomy", "model", "context", "git", "cwd"]


def test_hud_options_merge_over_preset_opts() -> None:
    rows = hud.resolve_config(_settings(hud_options={"model": {"provider": True}}))
    model_opts = next(opts for k, opts in rows[0] if k == "model")
    assert model_opts.get("provider") is True


def test_context_opts_carry_thresholds_from_settings() -> None:
    rows = hud.resolve_config(_settings(hud_context_warn=0.6, hud_context_crit=0.8))
    ctx_opts = next(opts for k, opts in rows[0] if k == "context")
    assert ctx_opts["warn"] == 0.6
    assert ctx_opts["crit"] == 0.8
