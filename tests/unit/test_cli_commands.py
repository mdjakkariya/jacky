"""Slash-command parsing + dispatch."""

from __future__ import annotations

from autobot.cli.commands import CommandResult, dispatch, parse


def test_parse_splits_name_and_args() -> None:
    assert parse("/model gpt-4") == ("/model", "gpt-4")
    assert parse("/help") == ("/help", "")


def test_parse_non_command_is_none() -> None:
    assert parse("just a request") is None
    assert parse("") is None


def test_help_lists_commands() -> None:
    res = dispatch("/help", "")
    assert res.action == "message" and "/exit" in res.text and "/clear" in res.text


def test_clear_and_exit_actions() -> None:
    assert dispatch("/clear", "") == CommandResult("clear")
    assert dispatch("/exit", "") == CommandResult("exit")


def test_unknown_command_hints() -> None:
    res = dispatch("/nope", "")
    assert res.action == "message" and "unknown" in res.text.lower()
