"""Tests for the logging setup: file creation, component tagging, idempotency."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from autobot.config import Settings
from autobot.logging_setup import get_logger, setup_logging


@pytest.fixture(autouse=True)
def _reset_logging_state() -> object:
    """Reset the module's one-time guard and handlers around each test."""
    import autobot.logging_setup as mod

    mod._configured = False
    logger = logging.getLogger("autobot")
    saved = logger.handlers[:]
    logger.handlers.clear()
    yield
    logger.handlers.clear()
    logger.handlers.extend(saved)
    mod._configured = False


def _settings(tmp_path: Path) -> Settings:
    return Settings(log_dir=str(tmp_path / "logs"), log_level="DEBUG")


def test_setup_creates_log_file(tmp_path: Path) -> None:
    log_path = setup_logging(_settings(tmp_path))
    assert log_path.exists()
    assert log_path.name == "autobot.log"


def test_lines_are_tagged_with_component(tmp_path: Path) -> None:
    log_path = setup_logging(_settings(tmp_path))
    get_logger("stt").info("transcribed chars=%d", 12)
    for handler in logging.getLogger("autobot").handlers:
        handler.flush()
    contents = log_path.read_text(encoding="utf-8")
    assert "[stt]" in contents
    assert "transcribed chars=12" in contents


def test_setup_is_idempotent(tmp_path: Path) -> None:
    setup_logging(_settings(tmp_path))
    handler_count = len(logging.getLogger("autobot").handlers)
    setup_logging(_settings(tmp_path))  # second call must not add handlers again
    assert len(logging.getLogger("autobot").handlers) == handler_count


def test_get_logger_is_namespaced() -> None:
    assert get_logger("gate").name == "autobot.gate"
