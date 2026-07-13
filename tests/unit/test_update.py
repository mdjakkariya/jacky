"""Pure cores behind `jack update` — platform mapping, throttle, atomic swap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autobot import update


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Darwin", "arm64", "jack-1.2.3-macos-arm64.tar.gz"),
        ("Darwin", "x86_64", "jack-1.2.3-macos-x64.tar.gz"),
        ("Linux", "x86_64", "jack-1.2.3-linux-x64.tar.gz"),
        ("Linux", "aarch64", "jack-1.2.3-linux-arm64.tar.gz"),
        ("Windows", "AMD64", "jack-1.2.3-windows-x64.zip"),
    ],
)
def test_asset_name(system: str, machine: str, expected: str) -> None:
    assert update.asset_name("1.2.3", system, machine) == expected


def test_asset_name_unknown_platform_raises() -> None:
    with pytest.raises(ValueError):
        update.asset_name("1.2.3", "Plan9", "sparc")


def test_version_gt() -> None:
    assert update.version_gt("0.7.0", "0.6.3")
    assert update.version_gt("1.0.0", "0.9.9")
    assert not update.version_gt("0.6.3", "0.6.3")
    assert not update.version_gt("0.6.2", "0.6.3")


def test_check_returns_newer_and_writes_cache(tmp_path: Path) -> None:
    cache = tmp_path / "c.json"
    got = update.check_for_update(
        "0.6.3", now=1000.0, cache_path=cache, fetch_latest=lambda: "0.7.0"
    )
    assert got == "0.7.0"
    saved = json.loads(cache.read_text())
    assert saved == {"last_check": 1000.0, "latest": "0.7.0"}


def test_check_none_when_current_is_latest(tmp_path: Path) -> None:
    got = update.check_for_update(
        "0.7.0", now=1.0, cache_path=tmp_path / "c.json", fetch_latest=lambda: "0.7.0"
    )
    assert got is None


def test_check_throttles_within_interval_and_skips_fetch(tmp_path: Path) -> None:
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"last_check": 1000.0, "latest": "0.7.0"}))

    def _boom() -> str | None:
        raise AssertionError("must not fetch within the interval")

    got = update.check_for_update(
        "0.6.3", now=1050.0, cache_path=cache, fetch_latest=_boom, interval_s=86400.0
    )
    assert got == "0.7.0"  # served from cache, no network


def test_check_fetches_again_after_interval(tmp_path: Path) -> None:
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"last_check": 1000.0, "latest": "0.6.3"}))
    got = update.check_for_update(
        "0.6.3", now=1000.0 + 90000.0, cache_path=cache, fetch_latest=lambda: "0.8.0"
    )
    assert got == "0.8.0"


def test_check_fetch_failure_returns_none(tmp_path: Path) -> None:
    got = update.check_for_update(
        "0.6.3", now=1.0, cache_path=tmp_path / "c.json", fetch_latest=lambda: None
    )
    assert got is None


def test_self_replace_swaps_atomically(tmp_path: Path) -> None:
    target = tmp_path / "jack"
    target.write_text("old")
    new = tmp_path / "jack.new"
    new.write_text("new")
    update.self_replace(new, target)
    assert target.read_text() == "new"
    assert not new.exists()


def test_sha256_of(tmp_path: Path) -> None:
    f = tmp_path / "x"
    f.write_bytes(b"abc")
    # sha256("abc")
    assert update.sha256_of(f) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_update_notice_only_when_newer() -> None:
    assert update.update_notice("0.7.0") == "▲ jack 0.7.0 is available — run 'jack update'"
    assert update.update_notice(None) is None
