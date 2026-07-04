# Coding-agent Phase 2f — coder profile + `jack` CLI (#53, v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make a coding turn runnable end-to-end from a terminal: `jack "add a docstring to foo.py"` → a warm **coder-profile** daemon → the code tools edit the file → the reply prints. This is the v1 "end-to-end CLI first" slice; plan mode, the autonomy dial, and `jack undo` are follow-on slices.

**Architecture:** A single new **`settings.profile`** field (`"assistant"` default | `"coder"`) is the switch. `app.build()` reads it: for `"coder"` it assembles a **lean registry of only the code tools** (`register_code_tools`, passing `command_allowlist`/`command_blocklist`) plus `register_builtins` (for `find_tools`), and **skips** the assistant tool blocks — crucially `register_file_io_tools`, whose `write_file`/`edit_file` names collide with the code tools. The three LLM adapters (which already hold `self._settings`) select a **coder system prompt** when `settings.profile == "coder"`. The daemon entry gains `--profile`/`--port` flags (so a coder daemon runs on its own port without clashing with the assistant/orb daemon). The **`jack` CLI** (`autobot/cli.py`, new `jack` console script) is a dependency-free `urllib` thin client: it ensures a coder daemon is up (spawning `python -m autobot.daemon --profile coder --port <p>` if not), then `POST /chat` and prints the reply. Cross-platform (no macOS-only calls).

**Tech Stack:** Python ≥ 3.11, stdlib only for the CLI (`urllib.request`, `subprocess`, `argparse`, `json`). Existing daemon (`daemon/server.py` `POST /chat`, `runner.serve`), `app.build`, `register_code_tools`, `system_prompt`.

## Global Constraints
- Conventional Commits; **NO Co-Authored-By / no AI-attribution trailer**; **no external-tool/product references** in code/docs. Stage explicit paths only.
- `make check` green before each task. `from __future__ import annotations`; mypy strict; line length 100; Google-style docstrings.
- **The `jack` CLI must be cross-platform** (Linux/Windows/macOS) — no macOS-only imports; use `subprocess`/`urllib` only.
- Behaviour-preserving for the existing assistant: `settings.profile` defaults to `"assistant"`, so `build()` and the adapters behave exactly as today unless `profile == "coder"`.
- Tools return strings, never raise; the CLI prints a friendly error (never a traceback) on connection failure.

## File Structure
- `src/autobot/config.py` — add `profile: str = "assistant"`.
- `src/autobot/llm/ollama_llm.py` — add `CODER_SYSTEM_PROMPT` + `system_prompt(mode, *, coder=False)`.
- `src/autobot/llm/anthropic_llm.py`, `src/autobot/agent/providers/openai_compatible.py`, `src/autobot/llm/ollama_llm.py` (Ollama adapter) — pass `coder=(self._settings.profile == "coder")` into their `system_prompt(...)` calls.
- `src/autobot/app.py` — `build()` coder branch (lean code-tool registry).
- `src/autobot/daemon/__main__.py` — `--profile` / `--port` flags → `serve` with an adjusted `Settings`.
- `src/autobot/cli.py` — the `jack` thin client (new). `pyproject.toml` — `jack = "autobot.cli:main"` script.
- Tests: `tests/unit/test_system_prompt_coder.py`, `tests/unit/test_build_coder_profile.py`, `tests/unit/test_cli.py` (+ extend daemon `__main__` test if one exists).

Deferred to later #53 slices (noted, out of scope here): plan-mode restricted preset, `coding_autonomy` (plan/confirm/auto) applied per coder turn, `jack undo`/`jack sessions`/`jack review` subcommands, repo-map context injection at turn start, streaming tokens.

---

### Task 1: `settings.profile` + coder system prompt

**Files:** Modify `config.py`, `llm/ollama_llm.py`, `llm/anthropic_llm.py`, `agent/providers/openai_compatible.py`. Test: `tests/unit/test_system_prompt_coder.py`.

**Interfaces produced:** `Settings.profile: str = "assistant"`; `system_prompt(mode: str, *, coder: bool = False) -> str`; `CODER_SYSTEM_PROMPT: str`.

- [ ] **Step 1: failing test** — Create `tests/unit/test_system_prompt_coder.py`:
```python
"""The coder system prompt is selected when coder=True."""

from __future__ import annotations

from autobot.llm.ollama_llm import system_prompt


def test_assistant_prompt_by_default() -> None:
    p = system_prompt("chat")
    assert "coding" not in p.lower() or "assistant" in p.lower()


def test_coder_prompt_when_coder_true() -> None:
    p = system_prompt("chat", coder=True)
    assert "code" in p.lower()
    # coder prompt still carries the chat delivery line (reply shown as text)
    assert p != system_prompt("chat", coder=False)


def test_coder_prompt_mentions_the_tools_workflow() -> None:
    p = system_prompt("chat", coder=True)
    low = p.lower()
    assert "read" in low and "edit" in low  # tells the model to read before editing
```

- [ ] **Step 2: run → FAIL** (`system_prompt` has no `coder` kwarg).

- [ ] **Step 3: implement.** In `config.py`, add to `Settings` (near the coding-agent block, after `command_blocklist`):
```python
    # Which agent this process is: "assistant" (voice/chat helper, default) or "coder"
    # (a code-editing agent — swaps in the code tools + a coding system prompt). Set by the
    # daemon's --profile flag or settings.json; the jack CLI runs a coder-profile daemon.
    profile: str = "assistant"
```
In `llm/ollama_llm.py`, add a coder prompt constant and branch `system_prompt` (keep the existing assistant `SYSTEM_PROMPT`):
```python
CODER_SYSTEM_PROMPT = (
    "You are a precise, autonomous coding agent working in a real code repository. "
    "Use the tools to inspect and change files: read a file (line-numbered) before you "
    "edit it, search with grep/glob, get an overview with repo_map, and run commands "
    "(tests, build, git) with run_command. Prefer the dedicated file tools over shelling "
    "out. Make the smallest change that satisfies the request, keep edits consistent with "
    "the surrounding code, and verify your work (run the tests) when practical. If a tool "
    "reports a failure, read the message and adjust rather than repeating the same call."
)


def system_prompt(mode: str, *, coder: bool = False) -> str:
    """The system prompt with a delivery line matched to the turn's mode.

    Args:
        mode: ``"chat"`` (reply shown as text) or anything else (spoken/voice).
        coder: when ``True``, use the coding-agent prompt instead of the assistant one.
    """
    base = CODER_SYSTEM_PROMPT if coder else SYSTEM_PROMPT
    delivery = CHAT_DELIVERY if mode == "chat" else VOICE_DELIVERY
    return f"{base}\n{delivery}"
```
In each of the three adapters, change their `system_prompt(session.delivery_mode)` call to `system_prompt(session.delivery_mode, coder=(self._settings.profile == "coder"))`:
- `agent/providers/openai_compatible.py:101` (in `_assemble`).
- `llm/ollama_llm.py:377` (Ollama adapter's assemble).
- `llm/anthropic_llm.py:517` (in `_system`).
Each adapter already stores `self._settings` — confirm and use it.

- [ ] **Step 4: run → PASS.** `uv run pytest tests/unit/test_system_prompt_coder.py -q`.
- [ ] **Step 5: `make check`** green (existing prompt tests unchanged — `coder` defaults False).
- [ ] **Step 6: commit**
```bash
git add src/autobot/config.py src/autobot/llm/ollama_llm.py src/autobot/llm/anthropic_llm.py src/autobot/agent/providers/openai_compatible.py tests/unit/test_system_prompt_coder.py
git commit -m "feat(agent): profile setting + coder system prompt (#53)"
```

---

### Task 2: `build()` coder branch (lean code-tool registry)

**Files:** Modify `src/autobot/app.py`. Test: `tests/unit/test_build_coder_profile.py`.

**Interfaces:** `build()` still returns an `Orchestrator`; when `Settings.load().profile == "coder"` the registry holds the code tools, not the assistant/fileio tools.

- [ ] **Step 1: failing test** — Create `tests/unit/test_build_coder_profile.py`:
```python
"""build() assembles a code-tool registry under the coder profile (no fileio name clash)."""

from __future__ import annotations

from pathlib import Path

from autobot.config import Settings
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.registry import ToolRegistry
from autobot.tools.code.tools import register_code_tools


class _FakeConfirmer:
    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return True

    def choose(self, prompt, options, kind="read", default="read"):  # type: ignore[no-untyped-def]
        return default


def test_code_tools_and_fileio_names_collide(tmp_path: Path) -> None:
    # This is WHY the coder profile needs its own registry: registering both raises.
    from autobot.tools.fileio import register_file_io_tools

    reg = ToolRegistry()
    pol = AccessPolicy(store_path=tmp_path / "a.json", workspace_root=tmp_path / "ws")
    broker = AccessBroker(pol, _FakeConfirmer())
    register_file_io_tools(reg, broker)
    try:
        register_code_tools(reg, broker)
        raised = False
    except ValueError:
        raised = True
    assert raised  # write_file/edit_file names clash — coder must use a separate registry


def test_coder_registry_has_code_tools(tmp_path: Path) -> None:
    reg = ToolRegistry()
    pol = AccessPolicy(store_path=tmp_path / "a.json", workspace_root=tmp_path / "ws")
    broker = AccessBroker(pol, _FakeConfirmer())
    register_code_tools(reg, broker, allowlist=[], blocklist=[])
    names = {s.name for s in reg.specs()}
    assert {"read_file", "edit_file", "grep", "run_command", "repo_map"} <= names
```
(These lock in the design contract; the `build()`-level assertion is covered by the smoke check in Step 4.)

- [ ] **Step 2: run → the collision test passes only after nothing else changes; the registry test passes.** Run to confirm both pass (they exercise existing functions) — they document the contract `build()` must honor.

- [ ] **Step 3: implement the `build()` branch.** In `app.py::build()`, at the tool-registration section (around app.py:459), branch on the profile:
```python
    coder = Settings.load().profile == "coder"
```
(or reuse the already-loaded `settings` variable in `build()` — use whichever `Settings` `build()` already has in scope). Then:
- For the **coder** path: after `broker` is constructed (it's built ~app.py:659, before `register_filesystem_tools`), register ONLY: `register_builtins(registry)` (already called at ~459 — keep it; it only adds `find_tools`/`get_time`, no clash) and `register_code_tools(registry, broker, allowlist=settings.command_allowlist, blocklist=settings.command_blocklist)`. **Skip** every `if settings.allow_*:` assistant block AND `register_file_io_tools` AND `register_filesystem_tools`/`register_workspace_tools` (workspace tools are assistant-scratch-oriented; the coder edits the real project via the code tools). Guard each assistant registration block with `if not coder and settings.allow_X:` (or wrap the whole assistant-tools section in `if not coder:`), and add the `register_code_tools(...)` call in an `if coder:` block right after `broker` exists.
- Everything else (access policy, gate, audit, `_build_llm`, orchestrator) is unchanged and shared.
Keep the change minimal and readable; a short comment marks the coder branch. Do NOT create a separate `build_coder()` — one `build()` with a profile branch keeps the daemon wiring simple.

- [ ] **Step 4: smoke test** — extend `test_build_coder_profile.py`:
```python
def test_build_with_coder_profile_registers_code_tools(tmp_path, monkeypatch) -> None:
    # Point Settings at a coder-profile file; build() must assemble code tools, not fileio.
    import autobot.app as app

    settings = Settings(profile="coder", sandbox_dir=str(tmp_path / "ws"),
                        access_store=str(tmp_path / "a.json"), audit_db=str(tmp_path / "a.db"),
                        agent_session_dir=str(tmp_path / "sess"), memory_db=str(tmp_path / "m.db"))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, *a, **k: settings))
    orch = app.build(settings)  # adjust to build()'s real signature
    reg = orch._registry  # white-box, consistent with existing test style
    names = {s.name for s in reg.specs()}
    assert "edit_file" in names and "run_command" in names
    assert "read_file_text" not in names  # the assistant's fileio tool is absent
```
Adjust to `build()`'s real signature/attribute names (read them first). If `build()` reads `Settings.load()` internally, the `monkeypatch` covers it; if it takes `settings`, pass it.

- [ ] **Step 5: run tests + `make check`** green (assistant path unchanged: `profile` defaults `"assistant"`).
- [ ] **Step 6: commit**
```bash
git add src/autobot/app.py tests/unit/test_build_coder_profile.py
git commit -m "feat(app): build a coder-profile registry (code tools only) (#53)"
```

---

### Task 3: daemon `--profile` / `--port` flags

**Files:** Modify `src/autobot/daemon/__main__.py` (and `runner.serve` only if needed to accept an override). Test: `tests/unit/test_daemon_main_args.py`.

**Interfaces:** `python -m autobot.daemon --profile coder --port 8766` runs a coder daemon on port 8766.

- [ ] **Step 1: failing test** — Create `tests/unit/test_daemon_main_args.py` that imports the arg parser from `daemon/__main__` and asserts `--profile coder --port 8766` parse to the right values, and that they map onto a `Settings` via `dataclasses.replace` (profile + daemon_port). Factor a pure `_parse_args(argv) -> argparse.Namespace` and a pure `_settings_from_args(base: Settings, args) -> Settings` in `__main__.py` so this is testable without starting a server:
```python
from autobot.config import Settings
from autobot.daemon.__main__ import _parse_args, _settings_from_args


def test_profile_and_port_flags() -> None:
    args = _parse_args(["--profile", "coder", "--port", "8766"])
    s = _settings_from_args(Settings(), args)
    assert s.profile == "coder"
    assert s.daemon_port == 8766


def test_defaults_keep_assistant() -> None:
    s = _settings_from_args(Settings(), _parse_args([]))
    assert s.profile == "assistant"
```

- [ ] **Step 2: run → FAIL** (`_parse_args`/`_settings_from_args` don't exist).

- [ ] **Step 3: implement.** In `daemon/__main__.py`, add `argparse` with `--profile` (default None → keep settings), `--port` (int, default None), keep the existing `--demo`. `_settings_from_args(base, args)` returns `replace(base, **overrides)` for the provided flags. `main()` loads `Settings.load()`, applies `_settings_from_args`, and calls `serve(settings)` (import lazily as today). Preserve the `--demo` → `serve_demo()` path and the missing-extra `SystemExit` message.

- [ ] **Step 4: run tests + `make check`** green.
- [ ] **Step 5: commit**
```bash
git add src/autobot/daemon/__main__.py tests/unit/test_daemon_main_args.py
git commit -m "feat(daemon): --profile and --port flags to run a coder daemon (#53)"
```

---

### Task 4: the `jack` CLI thin client

**Files:** Create `src/autobot/cli.py`. Modify `pyproject.toml` (`[project.scripts]`). Test: `tests/unit/test_cli.py`.

**Interfaces:** `jack "text"` → prints the coder daemon's reply; auto-spawns the daemon if down.

- [ ] **Step 1: failing test** — Create `tests/unit/test_cli.py`. Design `cli.py` so the network + spawn are injectable:
```python
from __future__ import annotations

import autobot.cli as cli


def test_send_chat_posts_and_returns_reply() -> None:
    seen = {}

    def fake_post(url: str, payload: dict, timeout: float) -> dict:
        seen["url"] = url
        seen["payload"] = payload
        return {"ok": True, "reply": "done"}

    reply = cli.send_chat("http://127.0.0.1:8766", "fix the bug", post=fake_post)
    assert reply == "done"
    assert seen["url"].endswith("/chat")
    assert seen["payload"] == {"text": "fix the bug"}


def test_send_chat_surfaces_error_reply() -> None:
    def fake_post(url, payload, timeout):  # type: ignore[no-untyped-def]
        return {"ok": False, "reply": "", "error": "chat unavailable"}

    reply = cli.send_chat("http://x", "hi", post=fake_post)
    assert "unavailable" in reply.lower() or "couldn" in reply.lower()


def test_daemon_up_probe(monkeypatch) -> None:
    # is_daemon_up returns True when the readiness probe succeeds, False on connection error.
    assert cli.is_daemon_up("http://x", probe=lambda url, timeout: True) is True
    assert cli.is_daemon_up("http://x", probe=lambda url, timeout: (_ for _ in ()).throw(OSError())) is False


def test_main_one_shot(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "ensure_daemon", lambda base: None)
    monkeypatch.setattr(cli, "send_chat", lambda base, text, **k: "the reply")
    rc = cli.main(["do a thing"])
    assert rc == 0
    assert "the reply" in capsys.readouterr().out
```

- [ ] **Step 2: run → FAIL** (no `cli` module).

- [ ] **Step 3: implement `src/autobot/cli.py`** (stdlib only):
```python
"""`jack` — a tiny cross-platform terminal client for the coder daemon.

Sends a coding request to a warm coder-profile daemon (spawning one on first use) and
prints the reply. Dependency-free: talks HTTP with ``urllib`` and spawns the daemon with
``subprocess``, so it runs the same on Linux, macOS, and Windows.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

_CODER_PORT = 8766  # coder daemon port (kept off the assistant daemon's 8765)
_SPAWN_TIMEOUT_S = 30.0


def _post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost only)
        body = resp.read().decode("utf-8")
    parsed: dict[str, Any] = json.loads(body)
    return parsed


def _probe(url: str, timeout: float) -> bool:
    with urllib.request.urlopen(url, timeout=timeout):  # noqa: S310
        return True


def is_daemon_up(base_url: str, probe: Callable[[str, float], bool] = _probe) -> bool:
    """True if a daemon answers a quick readiness probe at ``base_url``."""
    try:
        return probe(f"{base_url}/sessions", 1.0)
    except OSError:
        return False


def send_chat(
    base_url: str,
    text: str,
    post: Callable[[str, dict[str, Any], float], dict[str, Any]] = _post,
) -> str:
    """POST the request to the daemon's /chat and return the reply (or a friendly error)."""
    try:
        result = post(f"{base_url}/chat", {"text": text}, 600.0)
    except (OSError, urllib.error.URLError) as exc:
        return f"I couldn't reach the coder daemon: {exc}"
    if not result.get("ok"):
        return result.get("error") or result.get("reply") or "the coder daemon couldn't handle that."
    reply = result.get("reply")
    return reply if isinstance(reply, str) else ""


def ensure_daemon(base_url: str, port: int = _CODER_PORT) -> None:
    """Start a coder-profile daemon on ``port`` if one isn't already answering."""
    if is_daemon_up(base_url):
        return
    subprocess.Popen(  # noqa: S603 — fixed argv, our own module
        [sys.executable, "-m", "autobot.daemon", "--profile", "coder", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + _SPAWN_TIMEOUT_S
    while time.monotonic() < deadline:
        if is_daemon_up(base_url):
            return
        time.sleep(0.3)
    raise TimeoutError(f"coder daemon did not start on {base_url} within {_SPAWN_TIMEOUT_S:.0f}s")


def main(argv: list[str] | None = None) -> int:
    """`jack "…"` — send one coding request to the coder daemon and print the reply."""
    parser = argparse.ArgumentParser(prog="jack", description="Jack coding agent (terminal client).")
    parser.add_argument("text", nargs="+", help="the coding request, e.g. jack \"add a test for foo\"")
    parser.add_argument("--port", type=int, default=_CODER_PORT, help="coder daemon port")
    args = parser.parse_args(argv)
    base_url = f"http://127.0.0.1:{args.port}"
    text = " ".join(args.text)
    try:
        ensure_daemon(base_url, args.port)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(send_chat(base_url, text))
    return 0
```
Note: the `time.sleep` inside `ensure_daemon` is real spawn-wait (not in a hot loop); it's only exercised via the injected fakes in tests, never with a real sleep in the suite.

- [ ] **Step 4: add the console script** to `pyproject.toml` `[project.scripts]`:
```toml
jack = "autobot.cli:main"
```

- [ ] **Step 5: run tests + `make check`** green. `uv run pytest tests/unit/test_cli.py -q`.
- [ ] **Step 6: commit**
```bash
git add src/autobot/cli.py pyproject.toml tests/unit/test_cli.py
git commit -m "feat(cli): jack — cross-platform terminal client for the coder daemon (#53)"
```

---

## Notes for the executor
- **YAGNI:** v1 is a one-shot `jack "…"` end-to-end coding turn. Do NOT add plan mode, the autonomy dial, `jack undo`/`sessions`/`review` subcommands, streaming, or repo-map context injection (later slices — noted).
- **Name-clash is the crux:** the coder registry must contain the code tools and NOT `register_file_io_tools` (both define `write_file`/`edit_file`). Task 2's first test locks this in.
- **Cross-platform:** the CLI uses only `urllib`/`subprocess`/`argparse` — no macOS-only imports. The daemon spawn uses `sys.executable -m autobot.daemon`.
- **Behaviour-preserving:** `profile` defaults `"assistant"`; the assistant/orb path and all existing tests must be unchanged. If an existing test breaks, stop and report.
- **Manual end-to-end check (record in the final report, not a unit test):** in a throwaway git repo, `jack "create hello.py that prints hi"` should spawn the daemon, create the file via the code tools, and print a reply. (Requires a configured LLM provider; if none is available in the dev env, note that the pipe is verified by unit tests + a dry probe instead.)
