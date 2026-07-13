"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_update_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never hit GitHub for the update check during tests (hermetic).

    ``check_for_update`` only fetches (network) and writes the cache (filesystem) when
    ``fetch_latest`` returns something truthy, so stubbing it to ``None`` neutralizes
    both side effects for every test that ends up calling ``_print_update_notice``.
    """
    import autobot.update as update

    monkeypatch.setattr(update, "fetch_latest_version", lambda *a, **k: None)
