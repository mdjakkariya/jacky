# Developer shortcuts. Requires uv (https://docs.astral.sh/uv/).
# First time:  make setup

.DEFAULT_GOAL := help
.PHONY: help setup install lint format typecheck test ui-test check run hooks clean release-cli release-orb release-check-cli release-check-orb changelog-cli changelog-orb changelog-preview-cli changelog-preview-orb package-orb publish-orb freeze freeze-cli bundle voice

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: install hooks ## Create the env and install git hooks
	@echo "Setup complete. Run 'make run' to start (needs Ollama running)."

# `uv sync` REPLACES the installed extra set, so always sync the whole set at once.
EXTRAS := dev all daemon cloud whispercpp aec mcp tui
EXTRA_FLAGS := $(addprefix --extra ,$(EXTRAS))

install: ## Sync the virtualenv with all deps (dev + wake + tts + daemon + cloud + whispercpp + aec + mcp + tui)
	uv sync $(EXTRA_FLAGS)

install-cli: ## Install/update the system-wide `jack` from THIS source (uv tool; no venv needed)
	scripts/dev-install.sh

uninstall-cli: ## Remove the system-wide `jack` install
	scripts/dev-install.sh uninstall

lint: ## Lint with ruff
	uv run ruff check .

format: ## Auto-format with ruff
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Static type-check with mypy
	uv run mypy

test: ## Run the test suite with coverage
	uv run pytest

ui-test: ## Run the UI unit tests (Vitest + happy-dom; dev-only, needs Node ≥ 20)
	npm --prefix ui install
	npm --prefix ui test

check: ## Everything CI runs: lint + format check + types + tests
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest

run: ## Launch the assistant daemon
	uv run autobot-daemon

e2e: ## Dev-only: run the real-PTY E2E scenarios (needs `uv sync --extra e2e`)
	.venv/bin/python -m autobot.e2e $(if $(S),$(S),) $(if $(JUDGE),--judge $(JUDGE),)

dev-orb: ## Run the orb + drawer UI live from source (loads the CURRENT ui/orb). Run `make run` in another terminal for the daemon.
	@echo "Live UI from ui/orb — keep 'make run' (daemon on :8765) running in another terminal."
	@echo "Use this instead of a stale built Jack.app: 'make run' only restarts the daemon, NOT the UI."
	cd ui/orb-shell && cargo tauri dev

LOG ?= $(HOME)/.autobot/logs/autobot.log
logs: ## Tail the debug log (override path with LOG=…)
	tail -n 200 -f "$(LOG)"

logs-grep: ## Filter the log to one component, e.g. make logs-grep C=stt
	grep "\[$(C)\]" "$(LOG)"

release-cli: ## Bump the CLI/engine manifests: make release-cli VERSION=0.7.0
	uv run python scripts/bump_version.py cli $(VERSION)

release-orb: ## Bump the orb (src-tauri) manifests: make release-orb VERSION=0.3.0
	uv run python scripts/bump_version.py orb $(VERSION)

release-check-cli: ## Verify the CLI track agrees: make release-check-cli VERSION=0.7.0
	uv run python scripts/bump_version.py --check cli $(VERSION)

release-check-orb: ## Verify the orb track agrees: make release-check-orb VERSION=0.3.0
	uv run python scripts/bump_version.py --check orb $(VERSION)

changelog-cli: ## Prepend the CLI changelog section: make changelog-cli VERSION=0.7.0
	@command -v git-cliff >/dev/null || { echo "git-cliff not found — 'brew install git-cliff'."; exit 1; }
	@if [ -z "$(VERSION)" ]; then echo "Set VERSION, e.g. make changelog-cli VERSION=0.7.0"; exit 1; fi
	git-cliff --config cliff.cli.toml --unreleased --tag "v$(VERSION)" --prepend CHANGELOG-cli.md
	@echo "Updated CHANGELOG-cli.md with v$(VERSION). Review it, then commit."

changelog-orb: ## Prepend the orb changelog section: make changelog-orb VERSION=0.3.0
	@command -v git-cliff >/dev/null || { echo "git-cliff not found — 'brew install git-cliff'."; exit 1; }
	@if [ -z "$(VERSION)" ]; then echo "Set VERSION, e.g. make changelog-orb VERSION=0.3.0"; exit 1; fi
	git-cliff --config cliff.orb.toml --unreleased --tag "orb-v$(VERSION)" --prepend CHANGELOG-orb.md
	@echo "Updated CHANGELOG-orb.md with orb-v$(VERSION). Review it, then commit."

changelog-preview-cli: ## Print the pending CLI changelog without writing
	@command -v git-cliff >/dev/null || { echo "git-cliff not found — 'brew install git-cliff'."; exit 1; }
	@git-cliff --config cliff.cli.toml --unreleased

changelog-preview-orb: ## Print the pending orb changelog without writing
	@command -v git-cliff >/dev/null || { echo "git-cliff not found — 'brew install git-cliff'."; exit 1; }
	@git-cliff --config cliff.orb.toml --unreleased

freeze: ## Freeze the engine into dist/autobot-daemon (bundles the daemon/cloud/tts/wake/mcp deps)
	uv sync $(EXTRA_FLAGS) --extra freeze
	uv run pyinstaller --noconfirm --clean packaging/autobot-daemon.spec
	@echo "Built: dist/autobot-daemon"

freeze-cli: ## Freeze the coder CLI into dist/jack (lean: coder stack only, no voice)
	uv sync --inexact --extra tui --extra daemon --extra cloud --extra freeze
	uv run pyinstaller --noconfirm --clean packaging/jack.spec
	@echo "Built: dist/jack"

ORB_BUNDLE := ui/orb-shell/src-tauri/target/release/bundle
TARGET_TRIPLE := $(shell rustc -Vv 2>/dev/null | sed -n 's/host: //p')
SIDECAR_DIR := ui/orb-shell/src-tauri/binaries

# Default Piper voice bundled in the .app so a fresh install speaks out of the box.
# Matches tts_voice's default (en_US-ryan-high). Downloaded once into the (gitignored)
# resources dir; on first run the engine seeds it into ~/.autobot/voices.
VOICE_DIR := ui/orb-shell/src-tauri/voices
VOICE := en_US-ryan-high.onnx
VOICE_URL := https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high

voice: ## Download the bundled default Piper voice into the orb resources dir
	mkdir -p $(VOICE_DIR)
	[ -f "$(VOICE_DIR)/$(VOICE)" ] || curl -fSL "$(VOICE_URL)/$(VOICE)" -o "$(VOICE_DIR)/$(VOICE)"
	[ -f "$(VOICE_DIR)/$(VOICE).json" ] || curl -fSL "$(VOICE_URL)/$(VOICE).json" -o "$(VOICE_DIR)/$(VOICE).json"

SYSCAP_DIR := ui/orb-shell/src-tauri/syscap

build-syscap: ## Build the native system-audio sidecar (Swift; unsigned in dev — sign for release)
	cd autobot-syscap && swift build -c release
	mkdir -p $(SYSCAP_DIR)
	cp autobot-syscap/.build/release/autobot-syscap "$(SYSCAP_DIR)/autobot-syscap"
	@echo "Built + staged: $(SYSCAP_DIR)/autobot-syscap"
	@echo "RELEASE NOTE: before shipping, codesign it —"
	@echo "  codesign --force --options runtime --entitlements packaging/syscap.entitlements \\"
	@echo "    -s \"Developer ID Application: <YOUR ID>\" \"$(SYSCAP_DIR)/autobot-syscap\""
	@echo "  (unsigned: the macOS Audio-Capture prompt won't fire, so far-end capture degrades to mic-only.)"

bundle: freeze build-syscap ## Build the single .dmg: freeze engine -> sidecars -> tauri build (voice is NOT bundled — downloaded on demand when the user enables voice, ~115MB off the build)
	mkdir -p $(SIDECAR_DIR)
	cp dist/autobot-daemon "$(SIDECAR_DIR)/autobot-daemon-$(TARGET_TRIPLE)"
	cd ui/orb-shell && cargo tauri build
	@echo "Bundle (orb + engine + syscap, voice downloaded on demand) at $(ORB_BUNDLE)/dmg/"

package-orb: ## Build only the orb .dmg (assumes the sidecar is already in place)
	cd ui/orb-shell && cargo tauri build
	@echo "Built: $$(find $(ORB_BUNDLE)/dmg -name '*.dmg' 2>/dev/null | head -1)"

publish-orb: ## Upload the .dmg to the orb release with auto-generated notes: make publish-orb VERSION=0.3.0
	@dmg=$$(find $(ORB_BUNDLE)/dmg -name '*.dmg' 2>/dev/null | head -1); \
	if [ -z "$$dmg" ]; then echo "No .dmg found — run 'make package-orb' first."; exit 1; fi; \
	notes=$$(mktemp); \
	printf 'Jack orb **v%s** — dev preview. The .dmg is unsigned: right-click **Jack** → **Open**.\n\n' "$(VERSION)" > "$$notes"; \
	if command -v git-cliff >/dev/null 2>&1; then \
	  git-cliff --config cliff.orb.toml --latest --strip header >> "$$notes" \
	    || git-cliff --config cliff.orb.toml --unreleased --tag "orb-v$(VERSION)" --strip header >> "$$notes" || true; \
	else \
	  echo "(install git-cliff for auto-generated release notes)"; \
	fi; \
	if ! gh release view "orb-v$(VERSION)" >/dev/null 2>&1; then \
	  echo "Release orb-v$(VERSION) not found — creating it (tag from the pushed HEAD)…"; \
	  gh release create "orb-v$(VERSION)" --title "orb-v$(VERSION)" --notes-file "$$notes" \
	    || { echo "Couldn't create the release. Is 'gh auth login' done and HEAD pushed?"; rm -f "$$notes"; exit 1; }; \
	else \
	  gh release edit "orb-v$(VERSION)" --notes-file "$$notes" >/dev/null 2>&1 || true; \
	fi; \
	rm -f "$$notes"; \
	echo "Uploading $$dmg to release orb-v$(VERSION)…"; \
	gh release upload "orb-v$(VERSION)" "$$dmg" --clobber

hooks: ## Install pre-commit git hooks
	uv run pre-commit install

clean: ## Remove caches and build artifacts
	rm -rf .ruff_cache .mypy_cache .pytest_cache htmlcov .coverage build dist
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
