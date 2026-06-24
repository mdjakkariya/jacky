# Jack — Settings View, Config Overhaul & Optional Cloud LLM

Two linked goals:

1. **Config overhaul.** Replace environment variables with a single persisted
   `~/.autobot/settings.json` as the source of truth, edited through a **Settings
   view** in the orb. Secrets (API keys) are stored **encrypted** (macOS
   Keychain), never in the file. Precedence is simply **settings.json > defaults**
   — no env layer, no duplication.
2. **Optional cloud LLM.** Let the user swap Jack's brain from local Ollama to
   Anthropic Claude — opt-in, disclosed, default stays local.

## Why drop env

Today config is env-driven (`Settings.from_env`, `.env`, `_env_*` helpers,
`.env.example`). That means two places to define every tunable (the field default
and the env wiring) and a `.env` file users must hand-edit. Moving to one
`settings.json` written by the Settings view means: one source, no duplication,
easy to change/persist, and a real UI instead of editing dotfiles.

## Config model (new)

- **`~/.autobot/settings.json`** — the single config store. Missing file or
  missing key → the dataclass **field default**. That's the whole precedence:
  `settings.json > defaults`.
- `config.py` shrinks: `Settings` dataclass (defaults as today) + `Settings.load()`
  that reads the JSON and overlays known keys onto the defaults (ignoring unknown
  keys, coercing types, falling back on bad values). Remove `from_env`,
  `load_env_file`, the `_env_*` helpers, and `.env`/`.env.example`.
- **Secrets are NOT in settings.json.** API keys live in the **macOS Keychain**
  (via the `security` CLI or a tiny wrapper). settings.json stores only a boolean
  like `anthropic_key_set`; the value is fetched from Keychain at runtime. This is
  the "encrypt passwords" requirement done right — OS-managed encryption, not
  hand-rolled crypto, and the key never sits in a readable file.
- **Save semantics:** the Settings view writes settings.json (0600) and stores
  secrets in Keychain via the daemon; changes apply on **restart** (v1).

## Settings view (orb shell)

A real settings screen opened from a **Settings…** item in the orb's menu-bar — a
Tauri window/page (HTML form like `ui/orb`), talking to the local daemon. Every
configurable thing lives here, grouped:

- **Model** — provider (Local Ollama / Claude); model (Local: a dropdown of the
  **installed Ollama models** the daemon lists from `/api/tags`; Claude: model
  id); **API key** field (user-entered → Keychain); privacy note shown when cloud
  is selected.
- **Voice** — TTS on/off, voice model.
- **Listening** — input mode (wake/ptt), wake phrase, follow-up window,
  end-of-speech silence.
- **Capabilities** — app control, system info, memory, web search (+ its key →
  Keychain), each a toggle.
- **General/Debug** — reply length, session transcript on/off.

Daemon endpoints (our existing localhost API): `GET /settings` (current,
secrets shown only as "set/not set"), `GET /models` (installed Ollama models),
`POST /settings` (persist non-secrets), `POST /secret` (store an API key in
Keychain). Nothing leaves the machine.

## Optional cloud LLM (Claude)

### Privacy posture
- **Off by default** — default provider is local Ollama; with no change Jack is
  100% on-device.
- **What leaves the machine when cloud is on:** the conversation text, the
  injected **memory profile** (name + facts), tool *schemas*, and tool *results*
  go to Anthropic. **Voice never leaves** (STT/TTS local). **Actions never run in
  the cloud** — the cloud model only decides tool calls; execution stays on the
  Mac through the **local PermissionGate** (classify → confirm destructive →
  sandbox → audit).
- **Disclosed** at startup and in the Settings view; a sanctioned opt-in
  exception to "on-device only" (documented in CLAUDE.md alongside `web_search`).

### Architecture
The `LanguageModel` protocol (`run_turn(text, execute) -> reply`) already makes
the brain swappable — this is the swap it was designed for.
- **`llm/anthropic_llm.py`** — `AnthropicLanguageModel` implementing the protocol:
  system prompt + injected memory + history → Anthropic Messages API; advertise
  the registry tools (map our JSON-Schema `parameters` → Anthropic `input_schema`
  — a pure helper); on `tool_use`, run each through the **injected `execute`** (the
  same local gate), append `tool_result`, loop to a final reply. Pure helpers
  unit-tested with a fake client; **no network in tests**.
- **`build()` selects** the backend by `settings.llm_provider`; Keychain supplies
  the key when cloud. Gate, memory, tools, orb untouched.
- **Dependency:** official `anthropic` SDK as an optional extra
  (`uv sync --extra cloud`), lazy-imported.
- **Model:** a fast, strong tool-calling Claude (Haiku/Sonnet-class), configurable
  (not a coding model); names change, so it's a setting with a sensible default.

## Build steps

1. **Config overhaul:** `Settings.load()` from `settings.json` (settings.json >
   defaults); remove `from_env`/`load_env_file`/`_env_*`/`.env(.example)`; update
   all call sites (`app.build`, etc.) and rewrite `test_config.py` around the JSON
   store. Add a small Keychain helper (`secrets.py`) for get/set/delete.
2. **Daemon settings endpoints:** `GET/POST /settings`, `GET /models`,
   `POST /secret` reading/writing `settings.json` (0600) + Keychain.
3. **`anthropic_llm.py`** backend + pure schema-map/tool-parse helpers + tests.
4. **`build()`** provider switch + startup disclosure; CLAUDE.md exception + config
   note; pyproject `cloud` extra + mypy override.
5. **Settings view** in the orb shell (full grouped form + a **Settings…**
   menu-bar item); "restart to apply".
6. **Docs:** README updated for settings.json + Settings view; delete `.env`
   guidance.

## Out of scope (v1)

- **Live switching / live config reload** without restart (needs daemon-driven
  engine rebuild) — the view persists; applies on next start.
- **Other providers** (OpenAI-compatible, gateways) — the backend seam and the
  provider list are generic; Anthropic first.
- Streaming partial replies; non-macOS secret storage (Keychain is macOS-only —
  fine for the current target; a portable fallback can come later).
