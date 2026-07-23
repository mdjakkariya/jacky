"""Unit tests for the CLI HUD (state, segments, composition, config resolution)."""

from __future__ import annotations

from autobot.cli import hud
from autobot.cli.hud.segments import SEGMENTS, human_tokens
from autobot.cli.hud.state import HudState
from autobot.config import Settings


def _text(frags: list[tuple[str, str]] | None) -> str:
    assert frags is not None
    return "".join(t for _, t in frags)


def _render(
    key: str, state: HudState, opts: dict[str, object] | None = None, width: int = 200
) -> list[tuple[str, str]] | None:
    fn, _prio = SEGMENTS[key]
    return fn(state, opts or {}, width)


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


def test_autonomy_plain_and_mode() -> None:
    assert _text(_render("autonomy", HudState(autonomy="auto"))) == "auto"
    assert _text(_render("autonomy", HudState(autonomy="auto"), {"mode": True})) == "auto mode"


def test_model_provider_flag() -> None:
    s = HudState(model="claude-opus-4-8", provider="anthropic")
    assert _text(_render("model", s)) == "claude-opus-4-8"
    assert _text(_render("model", s, {"provider": True})) == "claude-opus-4-8 (anthropic)"


def test_context_none_before_data() -> None:
    assert _render("context", HudState(used=0, window=0)) is None


def test_context_pct_and_bar() -> None:
    s = HudState(used=38000, window=200000)
    assert _text(_render("context", s, {"style": "pct"})) == "ctx 19%"
    out = _text(_render("context", s, {"style": "bar+pct"}))
    assert out.startswith("ctx 19% ") and "░" in out


def test_context_color_thresholds() -> None:
    hot = _render("context", HudState(used=190000, window=200000), {"warn": 0.75, "crit": 0.9})
    assert hot is not None
    assert any("red" in style for style, _ in hot)
    warm = _render("context", HudState(used=160000, window=200000), {"warn": 0.75, "crit": 0.9})
    assert warm is not None
    assert any("amber" in style for style, _ in warm)


def test_tokens() -> None:
    assert _text(_render("tokens", HudState(used=38000, window=200000))) == "38k/200k"
    assert _render("tokens", HudState(used=0, window=0)) is None


def test_git_dirty_and_ahead_behind() -> None:
    s = HudState(branch="main", dirty=True, ahead=2, behind=0)
    assert _text(_render("git", s)) == "main*"
    assert _text(_render("git", s, {"ahead_behind": True})) == "main* ↑2"
    assert _render("git", HudState(branch="")) is None


def test_cwd() -> None:
    assert _text(_render("cwd", HudState(cwd="~/work/autobot"))) == "~/work/autobot"


def test_cost_and_label() -> None:
    assert _text(_render("cost", HudState(cost_usd=0.12))) == "$0.12"
    assert _text(_render("cost", HudState(cost_usd=0.12), {"label": "spend"})) == "spend $0.12"
    assert _render("cost", HudState(cost_usd=None)) is None


def test_mcp_and_skills_counts() -> None:
    assert _text(_render("mcp", HudState(mcp_count=6))) == "6 MCP"
    assert _render("mcp", HudState(mcp_count=None)) is None
    assert _render("skills", HudState(skills_count=None)) is None


def test_elapsed() -> None:
    assert _text(_render("elapsed", HudState(elapsed_s=90))) == "1m30s"
    assert _text(_render("elapsed", HudState(elapsed_s=45))) == "45s"
    assert _render("elapsed", HudState(elapsed_s=None)) is None


def test_human_tokens() -> None:
    assert human_tokens(200000) == "200k"
    assert human_tokens(38000) == "38k"
    assert human_tokens(500) == "500"
