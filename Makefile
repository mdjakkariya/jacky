# Developer shortcuts. Requires uv (https://docs.astral.sh/uv/).
# First time:  make setup

.DEFAULT_GOAL := help
.PHONY: help setup install lint format typecheck test check run hooks clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: install hooks ## Create the env and install git hooks
	@echo "Setup complete. Run 'make run' to start (needs Ollama running)."

install: ## Sync the virtualenv with all deps (dev + wake + tts + daemon)
	uv sync --extra dev --extra all --extra daemon

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

run: ## Launch the assistant (Ollama must be running)
	uv run autobot

LOG ?= $(HOME)/.autobot/logs/autobot.log
logs: ## Tail the debug log (override path with LOG=…)
	tail -n 200 -f "$(LOG)"

logs-grep: ## Filter the log to one component, e.g. make logs-grep C=stt
	grep "\[$(C)\]" "$(LOG)"

hooks: ## Install pre-commit git hooks
	uv run pre-commit install

clean: ## Remove caches and build artifacts
	rm -rf .ruff_cache .mypy_cache .pytest_cache htmlcov .coverage build dist
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
