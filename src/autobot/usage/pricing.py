"""Provider-agnostic price book: USD cost for a turn's tokens (or None if unknown).

A local estimate, not billing — the provider's console stays authoritative. Prices are
list rates per million tokens, keyed by ``provider`` then a model-id **prefix** (so 4.x
point releases match one entry). Prompt-cache tokens are priced on the input rate with
per-provider multipliers. Unknown ``(provider, model)`` returns ``None`` (tokens are still
recorded, just without a dollar figure) — never a fabricated ``$0``. Local providers price
to a real ``$0`` so "local, free" is distinct from "price unknown".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class Price:
    """List price for one model family (USD per 1M tokens), plus cache + intro rates."""

    input: float
    output: float
    cache_write_mult: float = 1.25  # x input rate (Anthropic 5-min cache write)
    cache_read_mult: float = 0.10  # x input rate (Anthropic cache read)
    intro_input: float | None = None
    intro_output: float | None = None
    intro_until: str | None = None  # inclusive ISO date (YYYY-MM-DD) the intro rate applies through


# Prefix-keyed, most-specific match wins. Extend as pricing changes; unknown -> None.
_PRICES: dict[str, dict[str, Price]] = {
    "anthropic": {
        "claude-sonnet-5": Price(
            3.0, 15.0, intro_input=2.0, intro_output=10.0, intro_until="2026-08-31"
        ),
        "claude-sonnet-4-6": Price(3.0, 15.0),
        "claude-haiku-4-5": Price(1.0, 5.0),
        "claude-opus-4": Price(5.0, 25.0),
        "claude-fable-5": Price(10.0, 50.0),
    },
    # Local: any model is free. The empty prefix matches every model id.
    "ollama": {"": Price(0.0, 0.0)},
    # Seed OpenAI entries here as needed; until then unknown OpenAI models are unpriced.
    "openai": {},
}


def _as_utc(at: datetime) -> datetime:
    """Interpret a naive datetime as UTC; convert an aware one to UTC."""
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)


def _lookup(provider: str, model: str) -> Price | None:
    """The longest matching model-prefix price for ``provider``, or None."""
    table = _PRICES.get(provider)
    if table is None:
        return None
    best: tuple[str, Price] | None = None
    for prefix, price in table.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, price)
    return best[1] if best else None


def price_usd(
    provider: str,
    model: str,
    *,
    in_tokens: int,
    out_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    at: datetime,
) -> float | None:
    """USD cost for one turn's token usage, or None when the model's price isn't known.

    Prices fresh input + output at the model's rate (the promotional rate when ``at`` falls
    on or before the entry's ``intro_until`` date), plus prompt-cache tokens at the cache
    multipliers on the input rate. ``at`` is the turn's timestamp (naive is treated as UTC).
    """
    price = _lookup(provider, model)
    if price is None:
        return None
    in_rate, out_rate = price.input, price.output
    if (
        price.intro_until is not None
        and price.intro_input is not None
        and price.intro_output is not None
        and _as_utc(at).date().isoformat() <= price.intro_until
    ):
        in_rate, out_rate = price.intro_input, price.intro_output
    return (
        in_tokens / 1_000_000 * in_rate
        + cache_write / 1_000_000 * in_rate * price.cache_write_mult
        + cache_read / 1_000_000 * in_rate * price.cache_read_mult
        + out_tokens / 1_000_000 * out_rate
    )
