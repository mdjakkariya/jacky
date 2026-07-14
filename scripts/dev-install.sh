#!/usr/bin/env bash
# Dev install / update: build the CURRENT local checkout of the jack CLI and install it
# system-wide via `uv tool`, so `jack` (outside this repo, with no venv active) runs your
# latest code. A clean `--force` reinstall replaces any previous version and refreshes the
# extras in one step.
#
# This is the from-SOURCE path — deliberately distinct from install.sh, which downloads a
# published *release* binary and would REPLACE your local build with an older tagged version.
#
# Usage:
#   scripts/dev-install.sh            # install/update from the current source
#   scripts/dev-install.sh uninstall  # remove the system install entirely
#   make install-cli / make uninstall-cli   # the same, via the Makefile
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f pyproject.toml ] || { echo "run this from the jack repo root" >&2; exit 1; }

# Coder CLI runtime extras — keep in sync with the uv-receipt:
#   tui    rich + prompt_toolkit (the inline shell)
#   daemon fastapi + uvicorn     (the local daemon the CLI drives)
#   cloud  anthropic             (the optional cloud LLM)
#   docs   pypdf/python-docx/openpyxl (@-mention extraction of pdf/docx/xlsx)
EXTRAS="tui,daemon,cloud,docs"

stop_daemons() {
  # Best-effort: stop any running jack daemon so a fresh CLI launch loads the new engine.
  pkill -f "autobot-daemon" 2>/dev/null || true
  pkill -f "autobot\.daemon" 2>/dev/null || true
}

if [ "${1:-}" = "uninstall" ]; then
  echo "▸ stopping any running jack daemon…"; stop_daemons
  echo "▸ removing the system install…"
  uv tool uninstall autobot 2>/dev/null || echo "  (nothing installed via uv tool)"
  echo "✓ uninstalled. (A ~/.local/bin/jack from install.sh, if any, is left untouched.)"
  exit 0
fi

command -v uv >/dev/null 2>&1 || {
  echo "uv is required — install it: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
}

echo "▸ stopping any running jack daemon (so the new engine loads)…"; stop_daemons

echo "▸ installing the current source: .[$EXTRAS]  (clean --force reinstall)…"
uv tool install --force ".[$EXTRAS]"

# Report the system install (the uv-tool one), not a venv-shadowed `jack`.
sys_jack="$HOME/.local/bin/jack"
echo
if [ -x "$sys_jack" ]; then
  echo "✓ installed $("$sys_jack" --version 2>/dev/null || echo '?')  →  $sys_jack"
else
  echo "✓ installed  →  $(uv tool dir)/autobot"
fi

# PATH hint.
case ":$PATH:" in
  *":$HOME/.local/bin:"*) : ;;
  *) echo "  add to PATH:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# Shadow note: an active .venv puts .venv/bin/jack first on PATH (also current, editable) —
# harmless, but that's why `jack` inside the repo may resolve there, not to the system copy.
if command -v jack >/dev/null 2>&1 && [ "$(command -v jack)" != "$sys_jack" ]; then
  echo "  note: 'jack' currently resolves to $(command -v jack)"
  echo "        (a .venv on PATH shadows the system copy — both are your latest code)."
fi

echo
echo "Run 'jack' in a project to start. Exit and relaunch any open jack to pick up changes."
