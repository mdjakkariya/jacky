"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolate_logging(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Never write the real ``~/.autobot/logs/autobot.log`` from a test (hermetic).

    ``setup_logging`` attaches a process-global rotating file handler to the user's log dir
    and is idempotent, so once any test triggers it (e.g. ``app.build()``), every later test's
    log lines leak into the real file — burying the user's real session logs (and their debug
    reports) under test noise. Reset the one-time guard + handlers around each test so nothing
    leaks across tests, and force ``app.build``'s ``setup_logging`` to a throwaway dir so even
    a build test's own lines stay isolated. (``test_logging_setup`` imports ``setup_logging``
    directly and has its own tmp fixture, so it's unaffected.)
    """
    import logging

    import autobot.app as app_mod
    import autobot.logging_setup as log_mod

    logdir = tmp_path_factory.mktemp("logs")
    real_setup = log_mod.setup_logging

    def _isolated(settings: Any) -> Any:
        from dataclasses import replace

        return real_setup(replace(settings, log_dir=str(logdir)))

    monkeypatch.setattr(app_mod, "setup_logging", _isolated)  # app imports it by name

    logger = logging.getLogger("autobot")
    saved = logger.handlers[:]
    logger.handlers.clear()
    log_mod._configured = False
    try:
        yield
    finally:
        logger.handlers.clear()
        logger.handlers.extend(saved)
        log_mod._configured = False


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
