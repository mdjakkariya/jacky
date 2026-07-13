"""The global workspace-trust store."""

from __future__ import annotations

from pathlib import Path

from autobot.trust import add_trust, is_trusted, remove_trust, trusted_folders


def test_untrusted_by_default(tmp_path: Path) -> None:
    assert is_trusted(tmp_path / "proj", path=tmp_path / "trust.json") is False


def test_add_then_trusted(tmp_path: Path) -> None:
    store = tmp_path / "trust.json"
    proj = tmp_path / "proj"
    proj.mkdir()
    add_trust(proj, path=store)
    assert is_trusted(proj, path=store) is True
    assert is_trusted(str(proj) + "/", path=store) is True  # resolves to the same folder
    assert str(proj.resolve()) in trusted_folders(path=store)


def test_add_is_idempotent(tmp_path: Path) -> None:
    store = tmp_path / "trust.json"
    proj = tmp_path / "proj"
    proj.mkdir()
    add_trust(proj, path=store)
    add_trust(proj, path=store)
    assert trusted_folders(path=store).count(str(proj.resolve())) == 1


def test_malformed_store_is_empty(tmp_path: Path) -> None:
    store = tmp_path / "trust.json"
    store.write_text("{not json", encoding="utf-8")
    assert trusted_folders(path=store) == []


def test_remove_trust(tmp_path: Path) -> None:
    store = tmp_path / "trust.json"
    proj = tmp_path / "proj"
    proj.mkdir()
    add_trust(proj, path=store)
    remove_trust(proj, path=store)
    assert is_trusted(proj, path=store) is False
    remove_trust(proj, path=store)  # idempotent (no error when absent)
