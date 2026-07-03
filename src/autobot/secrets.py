"""Cross-platform keyring-backed secret storage for API keys.

Secrets (e.g. an Anthropic / OpenAI / web-search API key) never touch
``settings.json`` or the logs — they live in the OS secret store via the
``keyring`` library: the login Keychain on macOS, Credential Locker on Windows,
and the Secret Service (libsecret) on Linux. All are stored under one service
name (``autobot``), keyed by an account name like ``anthropic_api_key``.

If no keyring backend is available (a headless Linux box with no Secret Service,
say), reads return ``None`` and writes fail gracefully, so the rest of the app
degrades cleanly. A ``backend`` is injectable so the logic is unit-tested without
touching a real keyring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    pass

_log = get_logger("app")

_SERVICE = "autobot"


class _Backend(Protocol):
    """The subset of the ``keyring`` API we use (so tests can inject a fake)."""

    def get_password(self, service: str, name: str) -> str | None: ...
    def set_password(self, service: str, name: str, value: str) -> None: ...
    def delete_password(self, service: str, name: str) -> None: ...


def _default_backend() -> _Backend:
    """The real ``keyring`` module (imported lazily so tests need no backend)."""
    import keyring

    return keyring


def get_secret(name: str, backend: _Backend | None = None) -> str | None:
    """Return the secret stored under ``name``, or ``None`` if not set/unavailable."""
    kr = backend or _default_backend()
    try:
        value = kr.get_password(_SERVICE, name)
    except Exception as exc:  # no backend, locked store, etc. — degrade cleanly
        _log.debug("keyring get failed for %s: %s", name, exc)
        return None
    return value or None


def set_secret(name: str, value: str, backend: _Backend | None = None) -> bool:
    """Store ``value`` under ``name`` (replacing any existing). Returns success."""
    kr = backend or _default_backend()
    try:
        kr.set_password(_SERVICE, name, value)
    except Exception as exc:
        _log.warning("keyring set failed for %s: %s", name, exc)
        return False
    return True


def delete_secret(name: str, backend: _Backend | None = None) -> bool:
    """Remove the secret stored under ``name``. Returns success (False if absent)."""
    kr = backend or _default_backend()
    try:
        kr.delete_password(_SERVICE, name)
    except Exception as exc:  # PasswordDeleteError when absent, or no backend
        _log.debug("keyring delete failed for %s: %s", name, exc)
        return False
    return True


def has_secret(name: str, backend: _Backend | None = None) -> bool:
    """Whether a secret is stored under ``name`` (without revealing it)."""
    return get_secret(name, backend) is not None
