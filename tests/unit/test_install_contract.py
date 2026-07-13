"""The install scripts and asset_name must agree on release-asset filenames."""

from __future__ import annotations

from pathlib import Path

from autobot import update

_ROOT = Path(__file__).resolve().parents[2]


def test_install_sh_uses_the_same_os_arch_tokens() -> None:
    sh = (_ROOT / "install.sh").read_text()
    low = sh.lower()
    # The five os/arch tokens asset_name emits must all be referenced (case-insensitively:
    # install.sh is Unix-only and mentions "windows" in the "use install.ps1" hint).
    for token in ("macos", "linux", "windows", "arm64", "x64"):
        assert token in low
    # And the exact filename shape jack update expects.
    assert "jack-${VERSION}-${OS}-${ARCH}.${EXT}" in sh


def test_asset_name_shape_matches_scripts() -> None:
    assert update.asset_name("1.0.0", "Linux", "x86_64") == "jack-1.0.0-linux-x64.tar.gz"
    assert update.asset_name("1.0.0", "Windows", "AMD64") == "jack-1.0.0-windows-x64.zip"
