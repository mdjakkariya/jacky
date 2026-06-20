"""Tests for the Keychain secret helper (no real Keychain touched)."""

from __future__ import annotations

from autobot.secrets import delete_secret, get_secret, has_secret, set_secret


class FakeKeychain:
    """Records `security` argv and returns canned results."""

    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.rc = rc
        self.out = out
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        return self.rc, self.out


def test_get_returns_value() -> None:
    kc = FakeKeychain(rc=0, out="sk-test-123\n")
    assert get_secret("anthropic_api_key", kc) == "sk-test-123"
    # service + account are passed; -w asks for the bare value.
    assert kc.calls[0][:2] == ["security", "find-generic-password"]
    assert "anthropic_api_key" in kc.calls[0]


def test_get_missing_returns_none() -> None:
    assert get_secret("nope", FakeKeychain(rc=44, out="not found")) is None


def test_get_empty_returns_none() -> None:
    assert get_secret("x", FakeKeychain(rc=0, out="")) is None


def test_get_when_security_unavailable_returns_none() -> None:
    assert get_secret("x", FakeKeychain(rc=127, out="security not found")) is None


def test_set_and_delete_report_success() -> None:
    kc = FakeKeychain(rc=0)
    assert set_secret("k", "v", kc) is True
    assert "-U" in kc.calls[0]  # updates if present
    assert delete_secret("k", kc) is True


def test_has_secret() -> None:
    assert has_secret("k", FakeKeychain(rc=0, out="v")) is True
    assert has_secret("k", FakeKeychain(rc=1, out="")) is False
