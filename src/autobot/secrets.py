"""macOS Keychain-backed secret storage for API keys.

Secrets (e.g. the Anthropic or web-search API key) never touch ``settings.json``
or the logs — they live in the login Keychain, encrypted by macOS. We shell out
to the ``security`` CLI under one service name (``autobot``), keyed by an account
name like ``anthropic_api_key``.

On a non-macOS host (or if ``security`` is unavailable), :func:`get_secret`
returns ``None`` and the setters no-op, so the rest of the app degrades cleanly.
A ``Runner`` is injected so the logic is unit-tested without touching a real
Keychain.
"""

from __future__ import annotations

from collections.abc import Callable

_SERVICE = "autobot"

RunResult = tuple[int, str]
Runner = Callable[[list[str]], RunResult]


def _subprocess_runner(args: list[str]) -> RunResult:
    """Default runner: run ``args`` (no shell) and return (code, output)."""
    import subprocess

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    except FileNotFoundError:
        return 127, "security not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def get_secret(name: str, runner: Runner | None = None) -> str | None:
    """Return the secret stored under ``name``, or ``None`` if not set/unavailable."""
    run = runner or _subprocess_runner
    rc, out = run(["security", "find-generic-password", "-s", _SERVICE, "-a", name, "-w"])
    if rc != 0:
        return None
    value = out.strip()
    return value or None


def set_secret(name: str, value: str, runner: Runner | None = None) -> bool:
    """Store ``value`` under ``name`` (replacing any existing). Returns success."""
    run = runner or _subprocess_runner
    # -U updates the item if it already exists.
    rc, _ = run(["security", "add-generic-password", "-U", "-s", _SERVICE, "-a", name, "-w", value])
    return rc == 0


def delete_secret(name: str, runner: Runner | None = None) -> bool:
    """Remove the secret stored under ``name``. Returns success."""
    run = runner or _subprocess_runner
    rc, _ = run(["security", "delete-generic-password", "-s", _SERVICE, "-a", name])
    return rc == 0


def has_secret(name: str, runner: Runner | None = None) -> bool:
    """Whether a secret is stored under ``name`` (without revealing it)."""
    return get_secret(name, runner) is not None
