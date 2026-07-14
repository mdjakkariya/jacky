"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_usage_ledger(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never write the real ``~/.autobot/usage.jsonl`` from a test (hermetic).

    ``record_turn``/``ledger.append`` default to :func:`autobot.usage.ledger.default_path`
    when no explicit path is given, and provider turns record with ``enabled=True`` — so any
    test that drives a full turn (fixtures use large token counts) would otherwise pollute the
    user's real cost ledger. Redirect the default to a per-test temp file. Tests that pass an
    explicit ``path=`` are unaffected; a test that needs its own target can still override
    ``default_path`` itself (its setattr runs after this and wins).
    """
    from autobot.usage import ledger

    target = tmp_path_factory.mktemp("usage") / "usage.jsonl"
    monkeypatch.setattr(ledger, "default_path", lambda: target)


@pytest.fixture(autouse=True)
def _no_update_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never hit GitHub for the update check during tests (hermetic).

    ``check_for_update`` only fetches (network) and writes the cache (filesystem) when
    ``fetch_latest`` returns something truthy, so stubbing it to ``None`` neutralizes
    both side effects for every test that ends up calling ``_print_update_notice``.
    """
    import autobot.update as update

    monkeypatch.setattr(update, "fetch_latest_version", lambda *a, **k: None)
