from __future__ import annotations

from autobot import secrets


class _FakeKeyring:
    """In-memory keyring backend: (service, name) -> value."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        # keyring raises PasswordDeleteError when absent; mimic by KeyError
        del self.store[(service, name)]


def test_set_then_get_roundtrips() -> None:
    kr = _FakeKeyring()
    assert secrets.set_secret("anthropic_api_key", "sk-123", backend=kr) is True
    assert secrets.get_secret("anthropic_api_key", backend=kr) == "sk-123"


def test_get_missing_returns_none() -> None:
    assert secrets.get_secret("nope", backend=_FakeKeyring()) is None


def test_has_secret_reflects_presence() -> None:
    kr = _FakeKeyring()
    assert secrets.has_secret("k", backend=kr) is False
    secrets.set_secret("k", "v", backend=kr)
    assert secrets.has_secret("k", backend=kr) is True


def test_delete_removes_and_is_safe_when_absent() -> None:
    kr = _FakeKeyring()
    secrets.set_secret("k", "v", backend=kr)
    assert secrets.delete_secret("k", backend=kr) is True
    assert secrets.get_secret("k", backend=kr) is None
    # deleting again must not raise, returns False
    assert secrets.delete_secret("k", backend=kr) is False


def test_empty_value_is_treated_as_absent() -> None:
    kr = _FakeKeyring()
    secrets.set_secret("k", "", backend=kr)
    assert secrets.get_secret("k", backend=kr) is None
