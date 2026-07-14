"""One call site per provider: price a finalized turn and append it to the ledger.

Provider-agnostic and best-effort — the whole body is guarded so a recording failure can
never crash a turn. ``enabled`` (the caller's ``Settings.usage_tracking``) makes the whole
thing a no-op when off.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from autobot.logging_setup import get_logger
from autobot.usage import ledger, pricing

_log = get_logger("usage")


def record_turn(
    *,
    provider: str,
    model: str,
    workspace: str,
    session_id: str,
    in_tokens: int,
    out_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    at: datetime,
    enabled: bool = True,
    path: Path | None = None,
) -> None:
    """Resolve this turn's cost and append one ledger row. Never raises.

    ``in_tokens`` must be the **raw fresh** input for the turn (not a display value that
    folds cache-write in) so cache tokens aren't double-counted.
    """
    if not enabled:
        return
    try:
        usd = pricing.price_usd(
            provider,
            model,
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
            at=at,
        )
        ledger.append(
            ledger.UsageEntry(
                ts=_iso(at),
                provider=provider,
                model=model,
                workspace=workspace,
                session_id=session_id,
                in_tokens=in_tokens,
                out_tokens=out_tokens,
                cache_read=cache_read,
                cache_write=cache_write,
                usd=usd,
                priced=usd is not None,
            ),
            path=path,
        )
        _log.debug(
            "recorded provider=%s model=%s in=%d out=%d usd=%s",
            provider,
            model,
            in_tokens,
            out_tokens,
            usd,
        )
    except Exception:  # recording must never break a turn
        _log.warning("usage recording failed provider=%s model=%s", provider, model, exc_info=True)


def _iso(at: datetime) -> str:
    """A UTC ISO string (``…Z``) for the ledger ``ts``."""
    aware = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")
