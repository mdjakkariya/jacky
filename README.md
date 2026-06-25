<div align="center">

# Jack

**Your Mac, with a voice. Entirely on-device.**

[![CI](https://img.shields.io/github/actions/workflow/status/mdjakkariya/jacky/ci.yml?branch=main&label=CI)](https://github.com/mdjakkariya/jacky/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/mdjakkariya/jacky/graph/badge.svg)](https://codecov.io/gh/mdjakkariya/jacky)
[![Latest release](https://img.shields.io/github/v/release/mdjakkariya/jacky)](https://github.com/mdjakkariya/jacky/releases)
[![Downloads](https://img.shields.io/github/downloads/mdjakkariya/jacky/total)](https://github.com/mdjakkariya/jacky/releases)
[![License](https://img.shields.io/github/license/mdjakkariya/jacky)](LICENSE)
[![Stars](https://img.shields.io/github/stars/mdjakkariya/jacky?style=flat)](https://github.com/mdjakkariya/jacky/stargazers)
![Platform](https://img.shields.io/badge/macOS-Apple%20Silicon-black?logo=apple)
![Views](https://visitor-badge.laobi.icu/badge?page_id=mdjakkariya.jacky)

**[Website](https://mdjakkariya.github.io/jacky/)** · **[Docs](https://mdjakkariya.github.io/jacky/getting-started.html)** · **[Download](https://github.com/mdjakkariya/jacky/releases)**

</div>

---

Jack is a privacy-first **voice & chat** assistant for macOS. You talk to a floating
orb (or type in a chat drawer); it listens, understands, and **acts on your Mac** —
open apps and websites, find and open files, check battery/Wi-Fi/disk, remember
things about you, empty the Trash. **Everything runs locally: no audio, text, or
memory leaves your machine**, with two clearly-marked, opt-in exceptions (web search
and an optional cloud LLM). English only.

## Highlights

- **Chat or voice** — type in the chat drawer (the default), or enable voice and say
  **“jack, …”** for hands-free (push-to-talk also supported).
- **Acts through a permission gate** — read-only tools run straight through,
  destructive ones ask first, and every action is audited. The model *plans*; the
  gate *executes* — it never runs a tool blindly.
- **Hands on your Mac** — app & web control, on-device file search (open / reveal in
  Finder), system info, and local memory.
- **Local by default, cloud optional** — runs fully on-device with Ollama, or switch
  to Claude (disclosed, opt-in).

→ Full feature tour, the orb's states, and how it works: **[jack's website](https://mdjakkariya.github.io/jacky/)**.

## Quickstart

**Download (recommended):** grab the latest **`.dmg`** from
[Releases](https://github.com/mdjakkariya/jacky/releases) — a single, self-contained
app. It's an unsigned dev preview, so the first launch is right-click **Jack** →
**Open**. Voice models download on demand the first time you enable voice.

**Build from source** (macOS, Apple Silicon):

```bash
brew install uv ollama
ollama serve && ollama pull qwen3:8b   # local LLM, one time
make setup                              # dev env + git hooks
make run                                # the engine/daemon
```

Then build the orb from `ui/orb-shell` (`cargo tauri dev`). Full setup, configuration,
permissions, and troubleshooting live in **[Getting started](https://mdjakkariya.github.io/jacky/getting-started.html)**.

## Architecture

A headless Python engine + a macOS **orb / chat** client (Tauri) that talks to it over
a localhost WebSocket. A pipeline of swappable stages drives one turn:

```
AudioSource ─▶ SpeechToText ─▶ LanguageModel ─plan─▶ PermissionGate ─▶ ToolRegistry
 wake+VAD/PTT   whisper         Ollama / Claude       risk·confirm·audit  apps·web·files·system
                                                                                  │
                              TextToSpeech (Piper) ◀──────── reply ◀──────────────┘
```

Diagram: [`docs/architecture/architecture.svg`](docs/architecture/architecture.svg).
Engineering conventions and the full design rationale are in [`CLAUDE.md`](CLAUDE.md).

## Star history

<a href="https://star-history.com/#mdjakkariya/jacky&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=mdjakkariya/jacky&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=mdjakkariya/jacky&type=Date" />
    <img alt="Star history chart for mdjakkariya/jacky" src="https://api.star-history.com/svg?repos=mdjakkariya/jacky&type=Date" width="640" />
  </picture>
</a>

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the workflow
(open an issue first, `make check`, Conventional Commits, DCO sign-off) and
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Sponsor

Jack is built in my free time. If it's useful to you, sponsoring helps me keep going via
**[GitHub Sponsors](https://github.com/sponsors/mdjakkariya)** — or buy me a chai:

[![Buy Me A Chai](https://buymeachai.ezee.li/assets/images/buymeachai-button.png)](https://buymeachai.ezee.li/mdjakkariya)

## License

Created and maintained by **Mohamed Jakkariya**
([@mdjakkariya](https://github.com/mdjakkariya)). Licensed under **Apache-2.0** — see
[`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). You're free to use, modify, and build on
Jack; please keep the attribution.
