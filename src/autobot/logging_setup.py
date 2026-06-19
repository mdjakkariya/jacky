r"""Central logging setup — a clean, filterable debug trail you can share.

Design goals:

* **Self-labeling.** Every line carries a ``[component]`` tag (e.g. ``[stt]``,
  ``[gate]``, ``[listening]``), so you can tell at a glance what each line is
  about — and filter to one feature with a single ``grep``::

      grep '\\[stt\\]'  ~/.autobot/logs/autobot.log
      grep '\\[gate\\]' ~/.autobot/logs/autobot.log

* **Signal, not noise.** Only our own ``autobot.*`` loggers are wired up
  (``propagate=False``), so third-party libraries (torch, whisper, ollama,
  sounddevice) never flood the file. Hot loops must not log per-frame.

* **Shareable.** One rotating file at ``~/.autobot/logs/autobot.log`` holds the
  whole trail at DEBUG; the console shows only WARNING+ so normal runs stay clean.

Get a component logger with :func:`get_logger` and log properties as ``key=value``
pairs so they're easy to read and grep::

    log = get_logger("stt")
    log.info("transcribed chars=%d confidence=%.2f latency_ms=%d", n, conf, ms)
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from autobot.config import Settings

_ROOT = "autobot"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(component)s] %(message)s"
_CONSOLE_FORMAT = "[%(component)s] %(levelname)s %(message)s"
_configured = False


class _ComponentFilter(logging.Filter):
    """Adds a short ``component`` field (the logger name minus the ``autobot.`` prefix)."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        component = name[len(_ROOT) + 1 :] if name.startswith(_ROOT + ".") else name
        # Set dynamically via __dict__ so the format string can use %(component)s.
        record.__dict__["component"] = component
        return True


def get_logger(component: str) -> logging.Logger:
    """Return the logger for a named component, e.g. ``get_logger("orchestrator")``.

    The component name becomes the ``[tag]`` on every line it logs, which is the
    handle you filter on when sharing logs.
    """
    return logging.getLogger(f"{_ROOT}.{component}")


def setup_logging(settings: Settings) -> Path:
    """Configure the ``autobot`` logger (idempotent). Returns the log file path."""
    global _configured
    log_path = Path(settings.log_dir).expanduser() / "autobot.log"
    if _configured:
        return log_path

    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(_ROOT)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # keep third-party logs out of our file

    component_filter = _ComponentFilter()

    file_handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(settings.log_level)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, "%Y-%m-%d %H:%M:%S"))
    file_handler.addFilter(component_filter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(settings.log_console_level)
    console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    console_handler.addFilter(component_filter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    _configured = True

    get_logger("log").info("logging started file=%s level=%s", log_path, settings.log_level)
    return log_path
