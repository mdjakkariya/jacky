"""Tests for the macOS Reminders tools (parsing + osascript via an injected runner)."""

from __future__ import annotations

from datetime import datetime

from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry
from autobot.tools.reminders import (
    RemindersTools,
    parse_due,
    register_reminders_tools,
)

# A fixed reference moment: Friday, 26 June 2026, 14:00 (2pm) local.
NOW = datetime(2026, 6, 26, 14, 0)


# --- parse_due -----------------------------------------------------------


def test_parse_relative_minutes_and_hours() -> None:
    assert parse_due("in 10 minutes", NOW) == datetime(2026, 6, 26, 14, 10)
    assert parse_due("in an hour", NOW) == datetime(2026, 6, 26, 15, 0)
    assert parse_due("in 2 hours", NOW) == datetime(2026, 6, 26, 16, 0)


def test_parse_relative_days_and_weeks() -> None:
    assert parse_due("in 3 days", NOW) == datetime(2026, 6, 29, 14, 0)
    assert parse_due("in a week", NOW) == datetime(2026, 7, 3, 14, 0)


def test_parse_tomorrow_with_and_without_time() -> None:
    assert parse_due("tomorrow at 9am", NOW) == datetime(2026, 6, 27, 9, 0)
    assert parse_due("tomorrow", NOW) == datetime(2026, 6, 27, 9, 0)  # default 9am


def test_parse_day_after_tomorrow() -> None:
    assert parse_due("day after tomorrow", NOW) == datetime(2026, 6, 28, 9, 0)


def test_parse_bare_hour_picks_next_future_occurrence() -> None:
    # "at 5" at 2pm -> 5am already passed, so 5pm today.
    assert parse_due("at 5", NOW) == datetime(2026, 6, 26, 17, 0)
    # "at 9" at 2pm -> 9am passed -> 9pm today.
    assert parse_due("at 9", NOW) == datetime(2026, 6, 26, 21, 0)


def test_parse_explicit_meridiem_and_24h() -> None:
    assert parse_due("5:30pm", NOW) == datetime(2026, 6, 26, 17, 30)
    assert parse_due("at 17:30", NOW) == datetime(2026, 6, 26, 17, 30)
    # 12pm == noon, already passed at 2pm, so it rolls to tomorrow.
    assert parse_due("12pm", NOW) == datetime(2026, 6, 27, 12, 0)


def test_parse_word_times_roll_when_passed() -> None:
    # noon (12:00) is before 2pm, so it rolls to tomorrow.
    assert parse_due("noon", NOW) == datetime(2026, 6, 27, 12, 0)
    # tonight pins to today's evening.
    assert parse_due("tonight", NOW) == datetime(2026, 6, 26, 19, 0)


def test_parse_named_weekday_and_next_week() -> None:
    # Friday -> next Monday is 29 June.
    assert parse_due("monday", NOW) == datetime(2026, 6, 29, 9, 0)
    assert parse_due("next monday at 10am", NOW) == datetime(2026, 6, 29, 10, 0)
    assert parse_due("next week", NOW) == datetime(2026, 7, 3, 9, 0)


def test_parse_named_day_leans_afternoon_for_early_bare_hour() -> None:
    # "tomorrow at 5" -> 5pm (afternoon heuristic on a named day).
    assert parse_due("tomorrow at 5", NOW) == datetime(2026, 6, 27, 17, 0)


def test_parse_unparseable_returns_none() -> None:
    assert parse_due("", NOW) is None
    assert parse_due("sometime later", NOW) is None
    assert parse_due("call the dentist", NOW) is None


# --- create_reminder -----------------------------------------------------


def test_create_with_due_passes_numeric_components() -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, "ok"

    out = RemindersTools(fake).create_reminder("call mom", "tomorrow at 9am", now=NOW)
    argv = seen[0]
    assert argv[:3] == ["osascript", "-e", argv[2]]  # script is arg 2
    assert argv[3] == "call mom"
    assert argv[4:] == ["2026", "6", "27", "9", "0"]
    assert "call mom" in out and "tomorrow at 9:00 AM" in out


def test_create_without_due_omits_date_args() -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, "ok"

    out = RemindersTools(fake).create_reminder("buy milk", now=NOW)
    assert len(seen[0]) == 4  # osascript, -e, script, title
    assert "buy milk" in out


def test_create_with_unparseable_due_still_creates_without_time() -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, "ok"

    out = RemindersTools(fake).create_reminder("water plants", "whenever", now=NOW)
    assert len(seen[0]) == 4  # no date components
    assert "left it open" in out.lower()


def test_create_empty_text_asks_what() -> None:
    out = RemindersTools(lambda _a: (0, "created")).create_reminder("", now=NOW)
    assert "remind you about" in out.lower()


def test_create_placeholder_title_asks_and_creates_nothing() -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, "created"

    out = RemindersTools(fake).create_reminder("Reminder", due="in 2 minutes", now=NOW)
    assert "remind you about" in out.lower()
    assert calls == []  # nothing was created


def test_create_upsert_reports_update_not_duplicate() -> None:
    out = RemindersTools(lambda _a: (0, "updated")).create_reminder(
        "call mom", "tomorrow at 9am", now=NOW
    )
    assert "already had" in out.lower() and "tomorrow at 9:00 AM" in out


def test_create_upsert_without_time_says_left_as_is() -> None:
    out = RemindersTools(lambda _a: (0, "updated")).create_reminder("call mom", now=NOW)
    assert "already have a reminder" in out.lower()


# --- update_reminder -----------------------------------------------------


def test_update_renames() -> None:
    out = RemindersTools(lambda _a: (0, "OK\tgroceries\t1")).update_reminder(
        "groceries", new_text="Buy groceries"
    )
    assert "Buy groceries" in out and "renamed" in out.lower()


def test_update_reschedules_and_passes_date_args() -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, "OK\tmeeting with manager\t1"

    out = RemindersTools(fake).update_reminder("meeting", due="tomorrow at 9am", now=NOW)
    assert seen[0][-5:] == ["2026", "6", "27", "9", "0"]  # date components appended
    assert seen[0][4] == ""  # empty new-title slot
    assert "tomorrow at 9:00 AM" in out


def test_update_needs_something_to_change() -> None:
    out = RemindersTools(lambda _a: (0, "")).update_reminder("meeting")
    assert "what should i change" in out.lower()


def test_update_not_found() -> None:
    out = RemindersTools(lambda _a: (0, "NONE")).update_reminder("ghost", due="at 5", now=NOW)
    assert "couldn't find" in out.lower()


def test_update_empty_query_asks() -> None:
    out = RemindersTools(lambda _a: (0, "")).update_reminder("", new_text="x")
    assert "which reminder" in out.lower()


# --- list_reminders ------------------------------------------------------


def test_list_formats_names_and_due_dates() -> None:
    raw = "call mom\tFriday, June 26, 2026 at 5:00:00 PM\nbuy milk\t\n"
    out = RemindersTools(lambda _a: (0, raw)).list_reminders()
    assert "call mom (due Friday, June 26, 2026 at 5:00:00 PM)" in out
    assert "buy milk" in out


def test_list_empty_is_friendly() -> None:
    out = RemindersTools(lambda _a: (0, "")).list_reminders()
    assert "no open reminders" in out.lower()


def test_list_passes_query_filter() -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, ""

    RemindersTools(fake).list_reminders("groceries")
    assert seen[0][-1] == "groceries"


# --- complete / delete ---------------------------------------------------


def test_complete_reports_name() -> None:
    out = RemindersTools(lambda _a: (0, "OK\tcall mom\t1")).complete_reminder("mom")
    assert "call mom" in out and "done" in out.lower()


def test_complete_notes_multiple_matches() -> None:
    out = RemindersTools(lambda _a: (0, "OK\tcall mom\t3")).complete_reminder("call")
    assert "3 matches" in out


def test_complete_not_found() -> None:
    out = RemindersTools(lambda _a: (0, "NONE")).complete_reminder("dentist")
    assert "couldn't find" in out.lower()


def test_complete_empty_query_asks() -> None:
    out = RemindersTools(lambda _a: (0, "")).complete_reminder("")
    assert "which reminder" in out.lower()


def test_delete_reports_name() -> None:
    out = RemindersTools(lambda _a: (0, "OK\told task\t1")).delete_reminder("old")
    assert "old task" in out and "deleted" in out.lower()


# --- error handling ------------------------------------------------------


def test_permission_error_returns_actionable_hint() -> None:
    denial = "Not authorized to send Apple events (-1743)"
    out = RemindersTools(lambda _a: (1, denial)).list_reminders()
    assert "permission" in out.lower() and "reminders" in out.lower()


def test_generic_failure_includes_detail() -> None:
    out = RemindersTools(lambda _a: (1, "boom")).create_reminder("x", now=NOW)
    assert "couldn't create" in out.lower() and "boom" in out


# --- registration --------------------------------------------------------


def test_register_adds_all_tools_with_expected_risk() -> None:
    registry = ToolRegistry()
    register_reminders_tools(registry, runner=lambda _a: (0, "ok"))
    assert registry.get("create_reminder").risk is Risk.WRITE  # type: ignore[union-attr]
    assert registry.get("update_reminder").risk is Risk.WRITE  # type: ignore[union-attr]
    assert registry.get("list_reminders").risk is Risk.READ_ONLY  # type: ignore[union-attr]
    assert registry.get("complete_reminder").risk is Risk.WRITE  # type: ignore[union-attr]
    assert registry.get("delete_reminder").risk is Risk.DESTRUCTIVE  # type: ignore[union-attr]
