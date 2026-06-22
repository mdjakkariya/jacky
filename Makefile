# Developer shortcuts. Requires uv (https://docs.astral.sh/uv/).
# First time:  make setup

.DEFAULT_GOAL := help
.PHONY: help setup install lint format typecheck test check run hooks clean release release-check package-orb publish-orb

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: install hooks ## Create the env and install git hooks
	@echo "Setup complete. Run 'make run' to start (needs Ollama running)."

install: ## Sync the virtualenv with all deps (dev + wake + tts + daemon + cloud + whispercpp + aec)
	uv sync --extra dev --extra all --extra daemon --extra cloud --extra whispercpp --extra aec

lint: ## Lint with ruff
	uv run ruff check .

format: ## Auto-format with ruff
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Static type-check with mypy
	uv run mypy

test: ## Run the test suite with coverage
	uv run pytest

check: ## Everything CI runs: lint + format check + types + tests
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest

run: ## Launch the assistant daemon
	uv run autobot-daemon

LOG ?= $(HOME)/.autobot/logs/autobot.log
logs: ## Tail the debug log (override path with LOG=…)
	tail -n 200 -f "$(LOG)"

logs-grep: ## Filter the log to one component, e.g. make logs-grep C=stt
	grep "\[$(C)\]" "$(LOG)"

release: ## Bump version in all manifests: make release VERSION=0.2.0
	uv run python scripts/bump_version.py $(VERSION)

release-check: ## Verify all manifests agree: make release-check VERSION=0.2.0
	uv run python scripts/bump_version.py --check $(VERSION)

ORB_BUNDLE := ui/orb-shell/src-tauri/target/release/bundle
package-orb: ## Build the macOS orb .dmg locally (needs cargo + tauri-cli)
	cd ui/orb-shell && cargo tauri build
	@echo "Built: $$(find $(ORB_BUNDLE)/dmg -name '*.dmg' 2>/dev/null | head -1)"

publish-orb: ## Upload the locally-built .dmg to the GitHub release: make publish-orb VERSION=0.2.0
	@dmg=$$(find $(ORB_BUNDLE)/dmg -name '*.dmg' 2>/dev/null | head -1); \
	if [ -z "$$dmg" ]; then echo "No .dmg found — run 'make package-orb' first."; exit 1; fi; \
	echo "Uploading $$dmg to release v$(VERSION)…"; \
	gh release upload "v$(VERSION)" "$$dmg" --clobber

hooks: ## Install pre-commit git hooks
	uv run pre-commit install

clean: ## Remove caches and build artifacts
	rm -rf .ruff_cache .mypy_cache .pytest_cache htmlcov .coverage build dist
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
