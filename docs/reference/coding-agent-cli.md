# Jack coding agent — CLI guide

`jack` is the terminal client for Jack's **coding agent**: a local agent that plans,
edits, and runs code in your project. It is a thin client of a **headless coder
daemon** — the CLI renders the TUI and streams events; the daemon owns the LLM, the
tools, and the permission gate. The same daemon backs the chat drawer, so behaviour is
identical across surfaces.

- Engine internals: [`architecture/design-reference.md`](architecture/design-reference.md)
- Config lives in `~/.autobot/settings.json`; **API keys never touch disk** — they live
  in the OS keyring (see [Providers & API keys](#providers--api-keys)).

## Quick start (cloud, Anthropic)

```bash
# 1. Install deps (cloud extra brings the Anthropic SDK)
uv sync --extra cloud

# 2. Store your API key in the OS keyring (service "autobot", account "anthropic_api_key")
keyring set autobot anthropic_api_key           # cross-platform; paste the key when prompted
# macOS one-liner alternative:
# security add-generic-password -U -s autobot -a anthropic_api_key -w 'sk-ant-…'

# 3. Point Jack at the cloud provider (settings.json defaults to local Ollama)
#    Edit ~/.autobot/settings.json:
#    { "llm_provider": "anthropic", "anthropic_model": "claude-sonnet-5" }

# 4. Run jack inside the project you want to work on
cd ~/code/my-project
jack                      # opens the interactive TUI
jack "add a docstring to foo.py"   # or run one request and print the reply
```

A new key takes effect on the **next turn** — no daemon restart. `jack` auto-spawns the
coder daemon on port **8766** the first time and reuses it afterward.

> The coder jails to the directory you launch it from (its cwd). Run `jack` from the
> repo you want it to edit.

## Providers & API keys

Jack is provider-agnostic. Pick one with `llm_provider` in `settings.json`; the key (if
any) is read from the keyring under a fixed account name. Only these secret names are
accepted: `anthropic_api_key`, `openai_api_key`, `web_api_key`.

| Provider | `settings.json` | Keyring account | Notes |
|---|---|---|---|
| **Anthropic** (cloud) | `"llm_provider": "anthropic"`, `"anthropic_model": "claude-sonnet-5"` | `anthropic_api_key` | Sends conversation + tool schemas/results to Anthropic; actions still run locally. |
| **OpenAI-compatible** | `"llm_provider": "openai"`, `"llm_model": "<model-id>"`, `"openai_base_url": "<endpoint>"` | `openai_api_key` | Any `chat.completions` endpoint (OpenAI, Groq, Together, Gemini's compat URL, local vLLM/LM Studio). Blank `openai_base_url` = OpenAI default. Local servers ignore the key. |
| **Ollama** (local, default) | `"llm_provider": "ollama"`, `"llm_model": "qwen3:8b"` | — | Fully on-device; Ollama must be running. |

Set a key from the keyring CLI:

```bash
keyring set autobot anthropic_api_key      # or openai_api_key
keyring get autobot anthropic_api_key      # verify
```

The GUI Settings view can also store keys (it posts to the daemon's `/secret`
endpoint) — same keyring, same effect.

## Running

- `jack` — open the interactive TUI (an inline REPL with streaming replies, live tool
  activity, plan/permission cards, and a working-tree diff after each change).
- `jack "<request>"` — one-shot: run a single coding turn, print the reply, exit.
- `jack --port <N>` — target a daemon on a non-default port.

## Autonomy modes

The `coding_autonomy` setting (or `/autonomy <value>` in-session) is a progressive-trust
dial. **All three** refuse blocklisted commands, stay within the cwd jail, and take a
git checkpoint at the start of every turn (so nothing is unrecoverable).

| Mode | Behaviour |
|---|---|
| `plan` (default) | Propose a numbered plan; act only after you approve it, then carry out the whole plan without re-prompting per step. |
| `confirm` | No plan phase; act directly, but ask before each command not on the allowlist. |
| `auto` | A lightweight router classifies each request and runs it as `plan` (multi-step/risky) or `confirm` (simple/low-risk); on any router failure it falls back to `plan`. |

## Slash commands

Type `/` in the TUI for completion. Client-side commands are instant; daemon-backed ones
(`/diff`, `/undo`, `/model`, `/autonomy`, `/sessions`, `/new`) query the daemon.

| Command | Does |
|---|---|
| `/help` | Show the command list |
| `/clear` | Clear the transcript (scrollback) |
| `/diff` | Show the working-tree diff (`git diff HEAD`) |
| `/undo` | Revert the last change; `/undo list` shows checkpoints |
| `/model` | Show or switch the model: `/model <name>` (applies next turn) |
| `/autonomy` | Show or set autonomy: `/autonomy plan\|confirm\|auto` |
| `/sessions` | List sessions; `/sessions resume <id>` to continue one |
| `/new` | Start a fresh session |
| `/exit` | Quit jack |

## Safety model

- **Permission gate.** Every acting tool (write/delete/run/network) is risk-classified and
  gated; destructive actions are confirmed and every decision is written to a SQLite audit
  log. The LLM never executes side effects directly.
- **cwd jail.** File tools are path-jailed to the launch directory; the agent cannot read
  or write outside it.
- **Checkpoints & undo.** With `checkpoints: true` (default), each turn snapshots the
  workspace to a private git ref (`refs/jack/checkpoints/<n>`); `/undo` rewinds to it.
- **Command policy.** `run_command` is layered on the gate: a built-in dangerous-pattern
  baseline plus your `command_allowlist` / `command_blocklist`. Blocklisted commands are
  refused outright; allowlisted patterns (e.g. `"git *"`, `"pytest*"`) run with less
  friction.
- **Secret redaction & egress disclosure.** Secrets are redacted from transcripts and
  logs. Cloud mode prints an explicit disclosure banner: your requests + remembered
  profile go to the provider, but audio never does and actions always run locally.

## Sessions

Sessions are workspace-scoped and persisted as JSONL transcripts under
`~/.autobot/agent_sessions/` (see `agent_session_dir`), with per-session cost tracking.
Use `/sessions` to list, `/sessions resume <id>` to continue, and `/new` to start fresh.

## Configuration reference (coder-relevant)

Edit `~/.autobot/settings.json` (defaults come from `config.py`):

| Key | Default | Purpose |
|---|---|---|
| `llm_provider` | `"ollama"` | `ollama` \| `anthropic` \| `openai` |
| `anthropic_model` | (default cloud model) | Model id when provider is `anthropic` |
| `llm_model` | (default local model) | Model id for `ollama` / `openai` |
| `openai_base_url` | `""` | Endpoint for the OpenAI-compatible provider |
| `coding_autonomy` | `"plan"` | `plan` \| `confirm` \| `auto` |
| `checkpoints` | `true` | Per-turn git snapshot for `/undo` |
| `command_allowlist` | `[]` | Patterns trusted to run with less friction |
| `command_blocklist` | `[]` | Patterns refused outright |
| `coder_llm_max_tokens` | `4096` | Output-token budget for coder turns |
| `agent_session_dir` | `~/.autobot/agent_sessions` | Where session transcripts live |

## Files & logs

- Settings: `~/.autobot/settings.json`
- API keys: OS keyring, service `autobot` (never on disk)
- Debug log: `~/.autobot/logs/autobot.log` — filter the coder with
  `grep '\[coder\]' ~/.autobot/logs/autobot.log`
- Sessions: `~/.autobot/agent_sessions/`
- Checkpoints: git refs `refs/jack/checkpoints/<n>` in the working repo

## For contributors: E2E verification

A dev-only PTY harness drives the real `jack` TUI through real LLM turns and scores each
run (deterministic checks + an LLM judge):

```bash
uv sync --extra e2e
python -m autobot.e2e                       # run the whole scenario corpus
python -m autobot.e2e create-file --judge auto   # one scenario, auto-judge
```

Artifacts (report, screen, raw transcript, steps, judge verdict) are written under
`~/.autobot/e2e/`. Add one scenario per bug report in
`src/autobot/e2e/scenarios/__init__.py`.
