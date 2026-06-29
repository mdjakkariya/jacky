"""Unit tests for autobot.mcp.client_secrets (pure stdlib; no SDK, no Keychain)."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest

import autobot.mcp.client_secrets as _cs


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    """Clear the lru_cache before and after every test to prevent bleed."""
    _cs._load.cache_clear()
    yield
    _cs._load.cache_clear()


# ---------------------------------------------------------------------------
# Core happy-path and edge-cases
# ---------------------------------------------------------------------------


def test_default_client_secret_returns_value_for_present_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File present with github key → default_client_secret("github") returns the secret."""
    secrets_file = tmp_path / "oauth_clients.json"
    secrets_file.write_text(json.dumps({"github": "s3cr3t"}), encoding="utf-8")
    monkeypatch.setenv("JACK_OAUTH_SECRETS_FILE", str(secrets_file))
    assert _cs.default_client_secret("github") == "s3cr3t"


def test_default_client_secret_returns_none_for_absent_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key not in file → default_client_secret returns None."""
    secrets_file = tmp_path / "oauth_clients.json"
    secrets_file.write_text(json.dumps({"github": "s3cr3t"}), encoding="utf-8")
    monkeypatch.setenv("JACK_OAUTH_SECRETS_FILE", str(secrets_file))
    assert _cs.default_client_secret("slack") is None


def test_default_client_secret_returns_none_when_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env points to a non-existent path → returns None (no raise)."""
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setenv("JACK_OAUTH_SECRETS_FILE", str(missing))
    assert _cs.default_client_secret("github") is None


def test_default_client_secret_returns_none_for_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed JSON → returns None, does not raise."""
    secrets_file = tmp_path / "oauth_clients.json"
    secrets_file.write_text("not-valid-json{{{", encoding="utf-8")
    monkeypatch.setenv("JACK_OAUTH_SECRETS_FILE", str(secrets_file))
    assert _cs.default_client_secret("github") is None


def test_default_client_secret_returns_none_for_non_dict_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON that parses to a list → treated as invalid, returns None."""
    secrets_file = tmp_path / "oauth_clients.json"
    secrets_file.write_text(json.dumps(["github", "slack"]), encoding="utf-8")
    monkeypatch.setenv("JACK_OAUTH_SECRETS_FILE", str(secrets_file))
    assert _cs.default_client_secret("github") is None


def test_cache_does_not_bleed_between_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sequential loads with different files produce different results (cache cleared)."""
    file_a = tmp_path / "a.json"
    file_a.write_text(json.dumps({"github": "secret-a"}), encoding="utf-8")
    monkeypatch.setenv("JACK_OAUTH_SECRETS_FILE", str(file_a))
    assert _cs.default_client_secret("github") == "secret-a"

    # Clear and switch to a different file
    _cs._load.cache_clear()
    file_b = tmp_path / "b.json"
    file_b.write_text(json.dumps({"github": "secret-b"}), encoding="utf-8")
    monkeypatch.setenv("JACK_OAUTH_SECRETS_FILE", str(file_b))
    assert _cs.default_client_secret("github") == "secret-b"
