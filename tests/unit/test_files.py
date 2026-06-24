"""Tests for the Spotlight file tools (pure ranking/formatting + injected runner)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autobot.tools.files import (
    _choice_items,
    _name_predicate,
    _rank,
    _tokens,
    format_results,
    open_path,
    reveal_path,
    search_files,
)


def test_tokens_splits_and_drops_quotes_and_slashes() -> None:
    assert _tokens('  "internship"/cert  ') == ["internship", "cert"]


def test_tokens_strips_filler_words() -> None:
    # 'a file with the name certificate' -> just the distinctive word
    assert _tokens("a file with the name certificate") == ["certificate"]


def test_tokens_keeps_raw_when_all_filler() -> None:
    # don't return nothing if the user literally searched for filler words
    assert _tokens("the file") == ["the", "file"]


def test_name_predicate_ands_every_token_case_insensitively() -> None:
    pred = _name_predicate(["a", "b"], "&&")
    assert pred == 'kMDItemFSName == "*a*"cd && kMDItemFSName == "*b*"cd'


def test_rank_prefers_more_matched_words_then_recency() -> None:
    paths = ["/h/only_cert.pdf", "/h/cert_internship.pdf", "/h/unrelated.txt"]
    mtimes = {"/h/only_cert.pdf": 100.0, "/h/cert_internship.pdf": 1.0}
    ranked = _rank(paths, ["cert", "internship"], lambda p: mtimes.get(p, 0.0))
    assert ranked[0] == "/h/cert_internship.pdf"  # 2 words beats recency


def test_rank_breaks_ties_by_recency() -> None:
    paths = ["/h/old_cert.pdf", "/h/new_cert.pdf"]
    mtimes = {"/h/old_cert.pdf": 1.0, "/h/new_cert.pdf": 999.0}
    ranked = _rank(paths, ["cert"], lambda p: mtimes.get(p, 0.0))
    assert ranked[0] == "/h/new_cert.pdf"


def test_format_results_numbered_with_open_hint() -> None:
    out = format_results("cert", ["/Users/me/a.pdf", "/Users/me/b.pdf"], total=2)
    assert "1. a.pdf" in out and "2. b.pdf" in out
    assert "which one to open" in out.lower()


def test_format_results_truncation_note() -> None:
    out = format_results("x", ["/h/a", "/h/b"], total=10)
    assert "showing the top 2" in out


def test_format_results_fuzzy_header() -> None:
    out = format_results("nope", ["/h/close.pdf"], total=1, fuzzy=True)
    assert "closest files" in out.lower()


def test_format_results_too_broad_asks_to_narrow() -> None:
    out = format_results("name", ["/h/a", "/h/b"], total=10789, fuzzy=True)
    assert "too broad" in out.lower()
    assert "more specific" in out.lower()
    assert "10789" in out


def test_format_results_empty_is_friendly() -> None:
    assert "couldn't find any files" in format_results("nope", []).lower()


def test_search_files_blank_query_asks_for_input() -> None:
    assert "look for" in search_files("   ", runner=lambda _a: (0, "")).lower()


def test_search_files_uses_and_predicate_and_formats_hits() -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, "/Users/me/cert_internship.pdf\n"

    out = search_files("internship cert", runner=fake, mtime_of=lambda _p: 0.0)
    assert seen[0][0] == "mdfind" and "-onlyin" in seen[0]
    assert "&&" in seen[0][-1]  # ANDs both words
    assert "cert_internship.pdf" in out


def test_search_files_falls_back_to_fuzzy_or_query() -> None:
    calls: list[str] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        calls.append(argv[-1])
        if "&&" in argv[-1]:
            return 0, ""  # no exact match
        return 0, "/Users/me/certificate_Mohamed.pdf\n"  # OR finds something

    out = search_files("internship certificate", runner=fake, mtime_of=lambda _p: 0.0)
    assert any("||" in c for c in calls)  # fuzzy fallback fired
    assert "closest files" in out.lower()
    assert "certificate_Mohamed.pdf" in out


def test_search_files_reports_runner_error() -> None:
    out = search_files("x", runner=lambda _a: (1, "boom"))
    assert "couldn't run the search" in out.lower() and "boom" in out


def test_open_path_blank_asks() -> None:
    assert "which file" in open_path("  ", runner=lambda _a: (0, "")).lower()


def test_open_path_missing_file() -> None:
    out = open_path("/nope/does/not/exist.pdf", runner=lambda _a: (0, ""))
    assert "can't find that file" in out.lower()


def test_open_path_opens_existing_file(tmp_path: Path) -> None:
    f = tmp_path / "report.pdf"
    f.write_text("x")
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, ""

    out = open_path(str(f), runner=fake)
    assert seen[0][0] == "open" and seen[0][1] == str(f)
    assert "opened report.pdf" in out.lower()


def test_open_path_reports_failure(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("x")
    out = open_path(str(f), runner=lambda _a: (1, "nope"))
    assert "couldn't open" in out.lower()


def test_reveal_path_uses_open_dash_r(tmp_path: Path) -> None:
    f = tmp_path / "r.pdf"
    f.write_text("x")
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, ""

    out = reveal_path(str(f), runner=fake)
    assert seen[0][:2] == ["open", "-R"] and seen[0][2] == str(f)
    assert "finder" in out.lower()


def test_reveal_path_missing_file() -> None:
    out = reveal_path("/nope/x.pdf", runner=lambda _a: (0, ""))
    assert "can't find that file" in out.lower()


def test_choice_items_have_open_reveal_copy_actions() -> None:
    items = _choice_items(["/Users/me/a.pdf"])
    assert items[0]["label"] == "a.pdf"
    tools = [a.get("tool") for a in items[0]["actions"]]
    assert "open_path" in tools and "reveal_path" in tools
    copy = [a for a in items[0]["actions"] if "copy" in a]
    assert copy and copy[0]["copy"] == "/Users/me/a.pdf"


def test_search_files_publishes_choices_card() -> None:
    published: list[Any] = []

    def sink(title: str, items: list[dict[str, Any]]) -> None:
        published.append((title, items))

    search_files(
        "report",
        runner=lambda _a: (0, "/Users/me/report.pdf\n"),
        mtime_of=lambda _p: 0.0,
        choices=sink,
    )
    assert published, "expected a choices card to be published"
    title, items = published[0]
    assert "report" in title and items[0]["label"] == "report.pdf"


def test_search_files_no_card_when_empty() -> None:
    published: list[Any] = []
    search_files(
        "nothinghere",
        runner=lambda _a: (0, ""),
        choices=lambda t, i: published.append((t, i)),
    )
    assert not published  # no results -> no card
