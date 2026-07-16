"""@path mention resolution: find, extract (bounded), and inject file context."""

from __future__ import annotations

from pathlib import Path

from autobot.cli import mentions


def test_find_mentions_ignores_emails_and_dedupes() -> None:
    text = "look at @src/a.py and @src/a.py, not user@host.com"
    assert mentions.find_mentions(text) == ["src/a.py"]


def test_find_mentions_strips_trailing_punctuation() -> None:
    assert mentions.find_mentions("check @notes.md.") == ["notes.md"]


def test_resolve_inlines_small_text_file(tmp_path: Path) -> None:
    (tmp_path / "hi.py").write_text("print('hello')\n", encoding="utf-8")
    out = mentions.resolve_mentions("explain @hi.py", str(tmp_path))
    assert "print('hello')" in out  # content inlined
    assert "@hi.py" in out  # labelled with the path
    assert out.rstrip().endswith("explain @hi.py")  # original message preserved after preamble


def test_resolve_no_mentions_is_unchanged(tmp_path: Path) -> None:
    assert mentions.resolve_mentions("just a message", str(tmp_path)) == "just a message"


def test_resolve_missing_file_notes_it(tmp_path: Path) -> None:
    out = mentions.resolve_mentions("see @nope.txt", str(tmp_path))
    assert "file not found" in out


def test_large_text_file_is_bounded_with_read_pointer(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("\n".join(f"line {i}" for i in range(5000)), encoding="utf-8")
    out = mentions.resolve_mentions("@big.txt", str(tmp_path), cap=2000)
    assert "read_file" in out  # pointer to read the rest
    assert "line 4999" in out  # tail preserved
    assert "elided" in out


def test_binary_file_gets_a_note_not_bytes(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
    out = mentions.extract_file(tmp_path / "blob.bin")
    assert "binary file" in out


def test_directory_mention_lists_its_contents(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    out = mentions.extract_file(tmp_path)
    assert "directory" in out and "sub/" in out and "a.txt" in out  # folders first, with a slash


def test_image_gets_a_vision_note(tmp_path: Path) -> None:
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n")
    out = mentions.extract_file(tmp_path / "logo.png")
    assert "image" in out and "vision" in out


def test_csv_inlines_as_text(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    out = mentions.extract_file(tmp_path / "data.csv")
    assert "a,b" in out and "1,2" in out


def test_at_most_max_mentions_resolved(tmp_path: Path) -> None:
    for i in range(mentions._MAX_MENTIONS + 3):
        (tmp_path / f"f{i}.txt").write_text(f"content {i}", encoding="utf-8")
    text = " ".join(f"@f{i}.txt" for i in range(mentions._MAX_MENTIONS + 3))
    out = mentions.resolve_mentions(text, str(tmp_path))
    assert "more attachments omitted" in out


def test_xlsx_extraction_round_trip(tmp_path: Path) -> None:
    import pytest

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["name", "qty"])
    ws.append(["widget", 3])
    path = tmp_path / "stock.xlsx"
    wb.save(path)
    out = mentions.extract_file(path)
    assert "name" in out and "widget" in out and "3" in out


def test_docx_extraction_round_trip(tmp_path: Path) -> None:
    import pytest

    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Hello from a Word doc.")
    path = tmp_path / "memo.docx"
    document.save(path)
    out = mentions.extract_file(path)
    assert "Hello from a Word doc." in out
