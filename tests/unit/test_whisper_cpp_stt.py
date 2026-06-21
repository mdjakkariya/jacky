"""Tests for the whisper.cpp STT segment parsing (no binary, no model)."""

from __future__ import annotations

from types import SimpleNamespace

from autobot.stt.whisper_cpp_stt import transcription_from_segments


def test_joins_object_segments() -> None:
    segments = [SimpleNamespace(text="Hey Jack,"), SimpleNamespace(text=" what's the time?")]
    out = transcription_from_segments(segments)
    assert out.text == "Hey Jack, what's the time?"
    assert out.confidence > 0.0


def test_joins_dict_segments() -> None:
    # Some binding versions yield dicts instead of objects.
    out = transcription_from_segments([{"text": "open"}, {"text": " spotify"}])
    assert out.text == "open spotify"


def test_empty_segments_yield_zero_confidence() -> None:
    out = transcription_from_segments([])
    assert out.text == "" and out.confidence == 0.0


def test_whitespace_only_is_treated_as_empty() -> None:
    out = transcription_from_segments([SimpleNamespace(text="   ")])
    assert out.text == "" and out.confidence == 0.0
