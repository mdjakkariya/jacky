"""Tests for the Spotlight file tools (pure ranking/formatting + injected runner)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from autobot.tools.files import (
    _choice_items,
    _kinds_present,
    _name_predicate,
    _prefix,
    _rank,
    _tokens,
    _type_predicate,
    format_results,
    open_path,
    reveal_path,
    search_files,
)


def test_type_predicate_maps_known_category_to_content_type() -> None:
    pred = _type_predicate("pdf")
    assert pred == 'kMDItemContentTypeTree == "com.adobe.pdf"'
    img = _type_predicate("photo")  # synonym -> image category
    assert img == 'kMDItemContentTypeTree == "public.image"'


def test_type_predicate_falls_back_to_extension() -> None:
    # Unknown-but-extension-shaped word -> match the filename extension.
    assert _type_predicate("xlsx") == 'kMDItemFSName == "*.xlsx"c'


def test_type_predicate_none_for_empty_or_unfilterable() -> None:
    assert _type_predicate(None) is None
    assert _type_predicate("   ") is None
    assert _type_predicate("something-not-a-type") is None


def test_kinds_present_lists_distinct_extensions_in_order() -> None:
    paths = ["/h/a.pdf", "/h/b.PDF", "/h/c.xlsx", "/h/d.md"]
    assert _kinds_present(paths) == [".pdf", ".xlsx", ".md"]


def test_prefix_drops_misheard_ending_but_keeps_short_tokens() -> None:
    assert _prefix("Autobot") == "Autob"  # 7 -> drop last 2 (start kept)
    assert _prefix("Samastor") == "Samast"
    assert _prefix("cert") == "cert"  # short tokens unchanged (would match too much)


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


def test_search_files_single_token_falls_back_to_prefix() -> None:
    # "Autobot" misheard: exact substring misses, but the prefix still finds the file.
    calls: list[str] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        pred = argv[-1]
        calls.append(pred)
        if '"*Autobot*"' in pred:
            return 0, ""  # no exact substring match
        if '"*Autob*"' in pred:  # prefix-relaxed
            return 0, "/Users/me/AutoBoard_notes.pdf\n"
        return 0, ""

    out = search_files("Autobot", runner=fake, mtime_of=lambda _p: 0.0)
    assert any('"*Autob*"' in c for c in calls)  # prefix tier fired
    assert "closest files" in out.lower()
    assert "AutoBoard_notes.pdf" in out


def test_search_files_prefix_and_tier_beats_or_explosion() -> None:
    # Two words, neither an exact match; the prefix-AND tier (narrow) must be tried
    # before widening to any-word OR (which would pull in unrelated common-word hits).
    seen: list[str] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        pred = argv[-1]
        seen.append(pred)
        if "&&" in pred and "*Samast*" in pred:  # prefix-relaxed AND
            return 0, "/Users/me/Samastra_certificate.pdf\n"
        if "&&" in pred:  # exact AND — no hit
            return 0, ""
        return 0, "/Users/me/unrelated_certificate.pdf\n"  # OR would over-match

    out = search_files("Samastor certificate", runner=fake, mtime_of=lambda _p: 0.0)
    assert not any("||" in p for p in seen)  # never needed the broad OR fallback
    assert "Samastra_certificate.pdf" in out
    assert "closest files" in out.lower()


def test_rank_scores_whole_word_above_prefix_only() -> None:
    # Exact-word match must rank above a file that only matches the relaxed prefix.
    paths = ["/h/AutoBoard.pdf", "/h/Autobot_final.pdf"]
    ranked = _rank(paths, ["Autobot"], lambda _p: 0.0)
    assert ranked[0] == "/h/Autobot_final.pdf"  # whole "autobot" beats prefix "autob"


def test_rank_prefers_word_boundary_match() -> None:
    # "cert" at a word boundary should beat the same letters buried mid-word.
    paths = ["/h/concert_tickets.pdf", "/h/my_cert.pdf"]
    ranked = _rank(paths, ["cert"], lambda _p: 0.0)
    assert ranked[0] == "/h/my_cert.pdf"  # boundary hit outranks "conCERT"


def test_rank_breaks_score_ties_by_shorter_name() -> None:
    paths = ["/h/certificate_of_completion_final.pdf", "/h/certificate.pdf"]
    ranked = _rank(paths, ["certificate"], lambda _p: 0.0)
    assert ranked[0] == "/h/certificate.pdf"  # shorter, less noise around the match


def test_search_files_applies_type_filter() -> None:
    seen: list[str] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        pred = argv[-1]
        seen.append(pred)
        return 0, "/Users/me/mohamed-certificate.pdf\n"

    out = search_files("certificate", file_type="pdf", runner=fake, mtime_of=lambda _p: 0.0)
    assert 'kMDItemContentTypeTree == "com.adobe.pdf"' in seen[0]  # type ANDed in
    assert "pdf file" in out.lower()
    assert "mohamed-certificate.pdf" in out


def test_search_files_type_missed_retries_without_type() -> None:
    # No PDF, but the name exists as other formats -> report the miss, show the rest.
    def fake(argv: list[str]) -> tuple[int, str]:
        pred = argv[-1]
        if "com.adobe.pdf" in pred:
            return 0, ""  # no PDFs match
        return 0, "/Users/me/mohamed-certificate.xlsx\n"

    out = search_files("certificate", file_type="pdf", runner=fake, mtime_of=lambda _p: 0.0)
    assert "didn't find any pdf" in out.lower()
    assert "mohamed-certificate.xlsx" in out


def test_search_files_broad_results_names_the_kinds_to_narrow() -> None:
    # A broad result spanning formats should invite narrowing by type.
    many = "".join(f"/Users/me/report_{i}.pdf\n" for i in range(70))
    many += "/Users/me/report_x.xlsx\n/Users/me/report_y.docx\n"

    out = search_files("report", runner=lambda _a: (0, many), mtime_of=lambda _p: 0.0)
    assert "too broad" in out.lower()
    assert "mix of" in out.lower()  # names the distinct extensions
    assert ".pdf" in out


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


def test_mdfind_runner_caps_and_terminates_quickly() -> None:
    import time as _time

    from autobot.tools.files import _MDFIND_MAX_LINES, _mdfind_runner

    # A process that would print forever; the runner must stop after the cap and return fast.
    argv = ["python3", "-c", "import sys\nwhile True: sys.stdout.write('x\\n')"]
    start = _time.monotonic()
    rc, out = _mdfind_runner(argv)
    elapsed = _time.monotonic() - start
    lines = out.splitlines()
    assert rc == 0
    assert len(lines) == _MDFIND_MAX_LINES  # capped, not unbounded
    assert elapsed < 15  # early-terminated, not run-to-completion (it never ends on its own)


def test_mdfind_runner_returns_all_when_few_and_exits_zero() -> None:
    from autobot.tools.files import _mdfind_runner

    rc, out = _mdfind_runner(["python3", "-c", "print('a'); print('b')"])
    assert rc == 0
    assert out.splitlines() == ["a", "b"]


def test_mdfind_runner_surfaces_error_when_no_output() -> None:
    from autobot.tools.files import _mdfind_runner

    rc, out = _mdfind_runner(["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"])
    assert rc == 2
    assert "boom" in out


def test_subprocess_runner_times_out() -> None:
    from autobot.tools.files import _subprocess_runner

    rc, out = _subprocess_runner(["sleep", "5"], timeout=0.5)
    assert rc == 124
    assert "timed out" in out.lower()


def test_mdfind_runner_bounded_when_producer_stalls_with_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as _time

    import autobot.tools.files as files_mod

    monkeypatch.setattr(files_mod, "_MDFIND_TIMEOUT_S", 0.5)
    # Emits one line, then hangs forever (never reaches the line cap, never EOFs).
    script = "import sys,time\nsys.stdout.write('a\\n'); sys.stdout.flush()\ntime.sleep(60)"
    argv = ["python3", "-c", script]
    start = _time.monotonic()
    _rc, out = files_mod._mdfind_runner(argv)
    elapsed = _time.monotonic() - start
    assert elapsed < 10  # bounded by the wall clock, not the 60s sleep
    assert "a" in out  # the line collected before the stall is returned


def test_mdfind_runner_times_out_with_no_output(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    import autobot.tools.files as files_mod

    monkeypatch.setattr(files_mod, "_MDFIND_TIMEOUT_S", 0.5)
    start = _time.monotonic()
    rc, out = files_mod._mdfind_runner(["sleep", "60"])  # never outputs, never EOFs in window
    elapsed = _time.monotonic() - start
    assert elapsed < 10
    assert rc == 124 and "timed out" in out.lower()
