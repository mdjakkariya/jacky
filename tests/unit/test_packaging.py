"""Guards that the optional MCP extra is actually shipped in the frozen bundle.

The engine imports ``mcp`` lazily (inside ``autobot.mcp.session``), so
PyInstaller's static analysis can't see it. For MCP to work in the bundled
``.dmg`` the package must be both (a) installed in the build env — the Makefile
``EXTRAS`` set, since ``make freeze`` runs ``uv sync $(EXTRA_FLAGS)`` and
``uv sync`` *replaces* the installed extra set — and (b) collected by the
PyInstaller spec. It broke once (v0.6.0: ``ModuleNotFoundError: No module named
'mcp'`` in the bundle) when the extra was added but never wired into the build;
these tests keep both ends wired.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _makefile_extras() -> list[str]:
    text = (_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^EXTRAS\s*:=\s*(.+)$", text, re.MULTILINE)
    assert match, "Makefile has no `EXTRAS :=` line"
    return match.group(1).split()


def _spec_collected_packages() -> list[str]:
    text = (_ROOT / "packaging" / "autobot-daemon.spec").read_text(encoding="utf-8")
    block = re.search(r"for _pkg in \((.*?)\):", text, re.DOTALL)
    assert block, "spec has no `for _pkg in (...)` collect_all block"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_build_env_installs_mcp_extra() -> None:
    """``make freeze`` must sync the ``mcp`` extra, else the bundle ships without it."""
    assert "mcp" in _makefile_extras()


def test_spec_collects_mcp() -> None:
    """The frozen daemon must collect the lazily-imported ``mcp`` package."""
    assert "mcp" in _spec_collected_packages()


def test_spec_collects_mcp_schema_data() -> None:
    """Collect the jsonschema ``.json`` meta-schemas PyInstaller misses by default."""
    collected = _spec_collected_packages()
    assert "jsonschema" in collected
    assert "jsonschema_specifications" in collected
