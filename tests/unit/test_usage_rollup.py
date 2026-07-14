"""Tests for pure rollup aggregation (deterministic — a fixed ``now`` is injected)."""

from __future__ import annotations

from datetime import datetime, timezone

from autobot.usage.ledger import UsageEntry
from autobot.usage.rollup import summarize

# Use midday UTC timestamps so local-vs-UTC bucketing can't straddle a date boundary.
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _e(
    ts: str,
    *,
    usd: float | None = 0.1,
    priced: bool = True,
    model: str = "claude-sonnet-5",
    provider: str = "anthropic",
    workspace: str = "/w",
    session_id: str = "s1",
) -> UsageEntry:
    return UsageEntry(
        ts=ts,
        provider=provider,
        model=model,
        workspace=workspace,
        session_id=session_id,
        in_tokens=10,
        out_tokens=20,
        cache_read=100,
        cache_write=50,
        usd=usd,
        priced=priced,
    )


def test_today_7d_30d_all_time_buckets() -> None:
    entries = [
        _e("2026-07-14T12:00:00Z"),  # today
        _e("2026-07-10T12:00:00Z"),  # within 7d
        _e("2026-06-30T12:00:00Z"),  # within 30d, not 7d
        _e("2026-01-01T12:00:00Z"),  # all-time only
    ]
    r = summarize(entries, now=NOW).to_dict()
    assert r["totals"]["today"]["turns"] == 1
    assert r["totals"]["last_7d"]["turns"] == 2
    assert r["totals"]["last_30d"]["turns"] == 3
    assert r["totals"]["all_time"]["turns"] == 4
    assert round(r["totals"]["all_time"]["usd"], 4) == 0.4


def test_tokens_and_cache_summed() -> None:
    r = summarize([_e("2026-07-14T12:00:00Z")], now=NOW).to_dict()
    t = r["totals"]["today"]
    assert t["in"] == 10 and t["out"] == 20 and t["tokens"] == 30
    assert t["cache_read"] == 100 and t["cache_write"] == 50


def test_unpriced_rows_flag_and_dont_add_dollars() -> None:
    r = summarize([_e("2026-07-14T12:00:00Z", usd=None, priced=False)], now=NOW).to_dict()
    assert r["totals"]["today"]["usd"] == 0.0
    assert r["totals"]["today"]["has_unpriced"] is True


def test_daily_series_is_zero_filled_for_30_days() -> None:
    r = summarize([_e("2026-07-14T12:00:00Z")], now=NOW).to_dict()
    assert len(r["daily"]) == 30
    assert r["daily"][-1]["date"] == "2026-07-14" and r["daily"][-1]["turns"] == 1
    assert r["daily"][0]["turns"] == 0  # 30 days ago, empty


def test_group_by_sorted_by_spend_desc() -> None:
    entries = [
        _e("2026-07-14T12:00:00Z", model="claude-haiku-4-5", usd=0.01),
        _e("2026-07-14T12:00:00Z", model="claude-opus-4-8", usd=0.50),
    ]
    r = summarize(entries, now=NOW).to_dict()
    assert [g["key"] for g in r["by_model"]] == ["claude-opus-4-8", "claude-haiku-4-5"]


def test_session_bucket_filters_by_session_id() -> None:
    entries = [
        _e("2026-07-14T12:00:00Z", session_id="live"),
        _e("2026-07-14T12:00:00Z", session_id="other"),
    ]
    r = summarize(entries, now=NOW, session_id="live").to_dict()
    assert r["session"]["turns"] == 1


def test_session_bucket_none_when_no_id() -> None:
    r = summarize([_e("2026-07-14T12:00:00Z")], now=NOW).to_dict()
    assert r["session"] is None
