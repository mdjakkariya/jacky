"""Choice parsers (pure) and the / + @ completer."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.cli import prompt


def test_slash_completer_offers_matching_commands() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    comp = prompt.JackCompleter({"/help": "h", "/clear": "c", "/exit": "e"}, cwd=".")
    doc = Document("/he", cursor_position=3)
    texts = [c.text for c in comp.get_completions(doc, None)]
    assert "/help" in texts and "/clear" not in texts


def test_file_completer_offers_cwd_paths(tmp_path: Path) -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    (tmp_path / "apples.py").write_text("x", encoding="utf-8")
    (tmp_path / "bananas.py").write_text("x", encoding="utf-8")
    comp = prompt.JackCompleter({}, cwd=str(tmp_path))
    doc = Document("fix @ap", cursor_position=7)
    texts = [c.text for c in comp.get_completions(doc, None)]
    assert any("apples.py" in t for t in texts)
    assert not any("bananas.py" in t for t in texts)


def test_file_completer_descends_into_subfolders(tmp_path: Path) -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    (tmp_path / "src" / "cli").mkdir(parents=True)
    (tmp_path / "src" / "cli" / "prompt.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "cli" / "render.py").write_text("x", encoding="utf-8")
    comp = prompt.JackCompleter({}, cwd=str(tmp_path))
    line = "open @src/cli/pro"
    doc = Document(line, cursor_position=len(line))
    texts = [c.text for c in comp.get_completions(doc, None)]
    assert "prompt.py" in texts  # nested file is reachable (the reported bug)
    assert "render.py" not in texts  # doesn't match the "pro" partial


def test_file_completer_shows_icons_and_folders_first(tmp_path: Path) -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    (tmp_path / "assets").mkdir()
    (tmp_path / "logo.png").write_text("x", encoding="utf-8")
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    comp = prompt.JackCompleter({}, cwd=str(tmp_path))
    doc = Document("@", cursor_position=1)
    comps = list(comp.get_completions(doc, None))
    assert comps[0].text == "assets/"  # folders sort first, with a trailing slash
    meta = {c.text: c.display_meta_text for c in comps}
    assert meta["assets/"] == "folder"
    assert meta["logo.png"] == "image"
    assert meta["notes.md"] == "markdown"
    display = {c.text: c.display_text for c in comps}
    assert "🖼️" in display["logo.png"] and "📁" in display["assets/"]
