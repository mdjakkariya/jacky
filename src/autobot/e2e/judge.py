"""Verify a run: deterministic checks (ground truth) + a natural-language LLM judge.

``run_checks`` machine-verifies files/screen. ``judge_auto`` asks a strong model whether the
run met the scenario's natural-language criteria and how the UX felt, returning a structured
verdict; ``parse_verdict`` is defensive (fenced/plain/garbage). The judge model is built from
the configured provider (``--judge-model`` overrides), so no key beyond what the coder uses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from autobot.e2e.scenario import Check, FileContains, FileExists, FileLacks, ScreenContains
from autobot.logging_setup import get_logger

_log = get_logger("e2e")

_JUDGE_PREAMBLE = (
    "You are grading an automated end-to-end test of a terminal coding assistant. Given the "
    "task, the success criteria, the final rendered terminal screen, and the deterministic "
    "check results, decide whether the run PASSED and critique the terminal UX.\n"
    "Reply with ONLY a raw JSON object and nothing else — no prose, no markdown fences, "
    "no leading or trailing text. The object must be exactly: "
    '{"pass": bool, "confidence": 0..1, "reasoning": str, "ux_notes": [str, ...]}.\n\n'
)


def run_checks(checks: list[Check], workspace: Path, screen: str) -> list[dict[str, Any]]:
    """Run each deterministic check against the workspace + final screen."""
    out: list[dict[str, Any]] = []
    for c in checks:
        if isinstance(c, FileExists):
            ok = (workspace / c.path).exists()
            out.append({"check": "FileExists", "path": c.path, "ok": ok})
        elif isinstance(c, FileContains):
            p = workspace / c.path
            ok = p.exists() and c.needle in p.read_text(encoding="utf-8", errors="replace")
            out.append({"check": "FileContains", "path": c.path, "needle": c.needle, "ok": ok})
        elif isinstance(c, FileLacks):
            p = workspace / c.path
            ok = p.exists() and c.needle not in p.read_text(encoding="utf-8", errors="replace")
            out.append({"check": "FileLacks", "path": c.path, "needle": c.needle, "ok": ok})
        elif isinstance(c, ScreenContains):
            out.append({"check": "ScreenContains", "needle": c.needle, "ok": c.needle in screen})
    return out


def build_judge_prompt(
    name: str, task: str, criteria: str, screen: str, checks: list[dict[str, Any]]
) -> str:
    """Compose the judge prompt from the run's artifacts."""
    return (
        f"{_JUDGE_PREAMBLE}"
        f"Scenario: {name}\nTask: {task}\nSuccess criteria: {criteria}\n\n"
        f"Deterministic checks:\n{json.dumps(checks, indent=2)}\n\n"
        f"Final terminal screen:\n```\n{screen}\n```\n"
    )


def parse_verdict(text: str) -> dict[str, Any]:
    """Parse a verdict JSON object from the model reply; safe fallback on failure.

    Robust to a strong model wrapping the JSON in prose or markdown fences: tries the
    whole (fence-stripped) reply first, then each ``{...}`` region, returning the first
    candidate that parses to a dict with a ``pass`` key.
    """
    candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith("```"):  # strip a ```json … ``` fence
        stripped = stripped.strip("`")
        stripped = stripped[4:] if stripped.lower().startswith("json") else stripped
    candidates.append(stripped.strip())
    candidates.extend(re.findall(r"\{.*?\}", text, re.DOTALL))  # each smallest {...}
    greedy = re.search(r"\{.*\}", text, re.DOTALL)  # first { … last }
    if greedy:
        candidates.append(greedy.group(0))
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict) and "pass" in obj:
            obj.setdefault("confidence", 0.0)
            obj.setdefault("reasoning", "")
            obj.setdefault("ux_notes", [])
            obj["pass"] = bool(obj["pass"])
            return obj
    return {
        "pass": False,
        "confidence": 0.0,
        "reasoning": "unparseable judge reply",
        "ux_notes": [],
        "raw": text[:500],
    }


def judge_auto(
    name: str,
    task: str,
    criteria: str,
    screen: str,
    checks: list[dict[str, Any]],
    *,
    judge_model: str | None = None,
) -> dict[str, Any]:
    """Ask a strong model for the qualitative verdict; return the parsed verdict."""
    from dataclasses import replace

    from autobot.app import _build_llm
    from autobot.config import Settings
    from autobot.session_log import NullTranscript
    from autobot.tools.registry import ToolRegistry

    s = Settings.load()
    if judge_model:
        if s.llm_provider == "anthropic":
            s = replace(s, anthropic_model=judge_model)
        else:
            s = replace(s, llm_model=judge_model)
    # Give the judge ample output room: the default assistant budget (512) truncates the
    # verdict JSON, and on adaptive-thinking models thinking also draws from max_tokens.
    s = replace(s, profile="assistant", anthropic_max_tokens=8192, llm_max_tokens=8192)
    try:
        model = _build_llm(s, ToolRegistry(), NullTranscript(), None)
        raw = model.complete(build_judge_prompt(name, task, criteria, screen, checks))
    except Exception as exc:  # never let judging crash a run — record it as a failed verdict
        _log.exception("judge_auto failed")
        return {
            "pass": False,
            "confidence": 0.0,
            "reasoning": f"judge error: {exc}",
            "ux_notes": [],
        }
    return parse_verdict(raw)
