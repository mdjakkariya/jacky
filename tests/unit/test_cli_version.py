"""`jack --version` prints the authoritative version."""

from __future__ import annotations

import autobot
from autobot.cli import main


def test_version_flag_prints_version(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"jack {autobot.__version__}"


def test_version_word_is_equivalent(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["version"]) == 0
    assert autobot.__version__ in capsys.readouterr().out
