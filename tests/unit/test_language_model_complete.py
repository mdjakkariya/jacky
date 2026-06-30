"""Tests for LanguageModel.complete protocol method.

Verifies that:
- A class WITHOUT complete does NOT satisfy the LanguageModel protocol.
- A class WITH complete DOES satisfy the protocol and the method works.
"""

from __future__ import annotations

from autobot.core.interfaces import LanguageModel


def test_fake_without_complete_fails_protocol() -> None:
    """A fake missing `complete` must NOT satisfy the LanguageModel protocol.

    This is the RED test: it fails before `complete` is added to the protocol
    (isinstance passes on extras alone with runtime_checkable) and passes after.
    """

    class NoComplete:
        def run_turn(self, user_text: str, execute: object) -> str:
            return ""

    assert not isinstance(NoComplete(), LanguageModel)


def test_fake_with_complete_satisfies_protocol() -> None:
    """A fake that has both run_turn and complete satisfies the protocol."""

    class Fake:
        def run_turn(self, user_text: str, execute: object) -> str:
            return ""

        def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
            return f"summary of {len(prompt)} chars"

    lm = Fake()
    assert isinstance(lm, LanguageModel)
    assert lm.complete("hello") == "summary of 5 chars"
