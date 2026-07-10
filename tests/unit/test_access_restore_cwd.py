from __future__ import annotations

import json
from pathlib import Path

from autobot.tools.access import AccessPolicy


def test_restore_cwd_false_keeps_workspace_cwd(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    other = tmp_path / "elsewhere"
    ws.mkdir()
    other.mkdir()
    # A persisted state that grants + points cwd at `other` (the bug scenario).
    store = tmp_path / "access.json"
    store.write_text(
        json.dumps({"cwd": str(other), "grants": [{"path": str(other), "mode": "write"}]}),
        encoding="utf-8",
    )
    # restore_cwd=True (default) would restore `other`; False must keep the workspace.
    restored = AccessPolicy(store, ws, restore_cwd=True)
    assert restored.cwd == other.resolve()
    pinned = AccessPolicy(store, ws, restore_cwd=False)
    assert pinned.cwd == ws.resolve()
