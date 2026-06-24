"""Tests for the debug breadcrumb buffer + report builder (autobot.diagnostics)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from autobot.diagnostics import (
    Crumb,
    DiagnosticsBuffer,
    RingLogHandler,
    build_report,
    redact,
)


@dataclass
class _FakeSettings:
    input_mode: str = "wake"
    wake_detector: str = "openwakeword"
    stt_engine: str = "faster_whisper"
    stt_model: str = "small.en"
    llm_provider: str = "ollama"
    llm_model: str = "qwen3:8b"
    anthropic_model: str = "claude-haiku-4-5"
    barge_in: bool = True
    aec: bool = False
    end_silence_ms: int = 1400
    max_utterance_s: float = 60.0
    tts_voice: str = "~/.autobot/voices/en_US-ryan-high.onnx"
    allow_web: bool = False
    allow_memory: bool = True


def _crumb(level: str, msg: str) -> Crumb:
    return Crumb(ts="12:00:00", level=level, component="test", message=msg)


def test_buffer_recent_is_bounded() -> None:
    buf = DiagnosticsBuffer(recent=3)
    for i in range(5):
        buf.add(_crumb("INFO", f"m{i}"))
    msgs = [c.message for c in buf.recent]
    assert msgs == ["m2", "m3", "m4"]  # only the last 3 kept


def test_buffer_errors_retained_separately() -> None:
    buf = DiagnosticsBuffer(recent=2, errors=10)
    buf.add(_crumb("ERROR", "boom"))
    for i in range(5):
        buf.add(_crumb("INFO", f"m{i}"))  # pushes the error out of `recent`
    assert all(c.level == "ERROR" for c in buf.errors)
    assert [c.message for c in buf.errors] == ["boom"]  # error survived
    assert "boom" not in [c.message for c in buf.recent]


def test_buffer_counts_by_level() -> None:
    buf = DiagnosticsBuffer()
    buf.add(_crumb("INFO", "a"))
    buf.add(_crumb("INFO", "b"))
    buf.add(_crumb("WARNING", "c"))
    assert buf.counts == {"INFO": 2, "WARNING": 1}


def test_buffer_state_trace() -> None:
    buf = DiagnosticsBuffer(states=2)
    buf.add_state("idle", "listening")
    buf.add_state("listening", "planning")
    buf.add_state("planning", "talking")
    trace = buf.states
    assert len(trace) == 2  # bounded
    assert trace[-1].endswith("planning→talking")


def test_ring_handler_captures_records() -> None:
    buf = DiagnosticsBuffer()
    handler = RingLogHandler(buf)
    logger = logging.getLogger("autobot.testcomp")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info("hello world")
        logger.error("kaboom")
    finally:
        logger.removeHandler(handler)
    msgs = [c.message for c in buf.recent]
    assert "hello world" in msgs
    assert "kaboom" in msgs
    assert [c.component for c in buf.recent] == ["testcomp", "testcomp"]
    assert [c.message for c in buf.errors] == ["kaboom"]


def test_redact_strips_keys_and_home() -> None:
    text = "key=sk-ant-api03-AAAA1111BBBB2222CCCC path=" + str(Path.home()) + "/x"
    out = redact(text)
    assert "sk-ant-api03" not in out
    assert "REDACTED" in out
    assert str(Path.home()) not in out
    assert "~/x" in out


def test_build_report_has_sections_and_is_redacted(tmp_path: Path) -> None:
    log = tmp_path / "autobot.log"
    log.write_text("2026-06-22 12:00:00 INFO    [app] started key sk-ant-SECRETSECRETSECRET\n")
    buf = DiagnosticsBuffer()
    buf.add_state("idle", "listening")
    buf.add(_crumb("ERROR", "an error happened"))

    report = build_report(_FakeSettings(), buffer=buf, log_path=log)  # type: ignore[arg-type]

    for heading in (
        "# Jack debug report",
        "## Config",
        "## State sequence",
        "## Errors & warnings",
        "## Recent events",
        "## Log tail",
    ):
        assert heading in report
    assert "idle→listening" in report
    assert "an error happened" in report
    assert "llm_provider: ollama" in report
    assert "sk-ant-SECRET" not in report  # redacted from the log tail
