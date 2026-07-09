"""Pure config parsing/validation for the ``jack config`` CLI (no file/daemon/keyring I/O).

Everything here is a pure function over strings/dicts so the CLI's validation is unit-tested
without touching disk, the keyring, or the daemon. Valid keys and their types are derived
from the ``Settings`` dataclass, so this never drifts from ``config.py``.
"""

from __future__ import annotations

from autobot.config import Settings, setting_names

# Short, friendly names for the common settings. Raw dataclass field names always work too.
ALIASES: dict[str, str] = {"provider": "llm_provider", "autonomy": "coding_autonomy"}

# Curated enums (the dataclass doesn't encode them).
ENUMS: dict[str, frozenset[str]] = {
    "llm_provider": frozenset({"ollama", "anthropic", "openai"}),
    "coding_autonomy": frozenset({"plan", "confirm", "auto"}),
}

# Provider name -> keyring account for ``jack config set-key``.
KEY_SECRETS: dict[str, str] = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "web": "web_api_key",
}


class ConfigError(Exception):
    """A user-facing configuration error (bad key, value, or file)."""


def resolve_key(name: str, *, provider: str) -> str:
    """Map an alias or raw name to a canonical ``Settings`` field name.

    ``model`` is provider-aware: it resolves to ``anthropic_model`` when ``provider`` is
    ``anthropic``, otherwise ``llm_model``. Raises :class:`ConfigError` for unknown names.
    """
    if name == "model":
        return "anthropic_model" if provider == "anthropic" else "llm_model"
    canonical = ALIASES.get(name, name)
    if canonical not in setting_names():
        valid = ", ".join(sorted(setting_names() | set(ALIASES) | {"model"}))
        raise ConfigError(f"unknown setting {name!r}. Valid keys: {valid}")
    return canonical


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def coerce_value(key: str, raw: str, *, defaults: Settings) -> object:
    """Coerce a CLI string to the field's Python type; raise :class:`ConfigError` on bad input."""
    default = getattr(defaults, key)
    expected = type(default)
    if expected is bool:
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"{key} expects a boolean (true/false), got {raw!r}")
    if expected is int:
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(f"{key} expects an integer, got {raw!r}") from None
        if value <= 0 and ("max_tokens" in key or "timeout" in key):
            raise ConfigError(f"{key} must be greater than 0, got {value}")
        return value
    if expected is float:
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"{key} expects a number, got {raw!r}") from None
    if expected is list:
        return [item.strip() for item in raw.split(",") if item.strip()]
    # str
    if raw == "" and default != "":
        raise ConfigError(f"{key} cannot be empty")
    return raw


def validate(key: str, value: object) -> None:
    """Enforce enum membership for keys that have a fixed value set."""
    allowed = ENUMS.get(key)
    if allowed is not None and value not in allowed:
        raise ConfigError(
            f"invalid value {value!r} for {key}; allowed: {', '.join(sorted(allowed))}"
        )


def prepare_set(
    name: str, raw: str, *, current: dict[str, object], defaults: Settings
) -> tuple[str, object]:
    """Resolve the key (provider-aware), coerce the value, validate it. Returns (key, value)."""
    provider = str(current.get("llm_provider", defaults.llm_provider))
    key = resolve_key(name, provider=provider)
    value = coerce_value(key, raw, defaults=defaults)
    validate(key, value)
    return key, value


def format_settings(effective: dict[str, object], secrets: dict[str, bool]) -> str:
    """Render settings as ``key = value`` lines plus a secrets set/unset block."""
    lines = [f"{key} = {value}" for key, value in sorted(effective.items()) if key != "_secrets"]
    if secrets:
        lines.append("")
        lines.append("secrets:")
        lines += [
            f"  {name}: {'set' if present else 'unset'}"
            for name, present in sorted(secrets.items())
        ]
    return "\n".join(lines)
