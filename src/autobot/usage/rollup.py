"""Pure aggregation of ledger entries into the shape both surfaces render.

No I/O and no hidden clock — ``now`` is injected so results are deterministic. Days bucket
by **local** date (what a human means by "today"); each entry's UTC ``ts`` is converted to
the system's local time zone before its date is taken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from autobot.usage.ledger import UsageEntry

_DAILY_DAYS = 30


@dataclass(slots=True)
class Bucket:
    """A running total over a set of entries."""

    turns: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    usd: float = 0.0
    has_unpriced: bool = False

    def add(self, entry: UsageEntry) -> None:
        """Fold one entry into the totals."""
        self.turns += 1
        self.in_tokens += entry.in_tokens
        self.out_tokens += entry.out_tokens
        self.cache_read += entry.cache_read
        self.cache_write += entry.cache_write
        if entry.usd is not None:
            self.usd += entry.usd
        if not entry.priced:
            self.has_unpriced = True

    def to_dict(self) -> dict[str, Any]:
        """The transport shape (``tokens`` = in + out)."""
        return {
            "turns": self.turns,
            "in": self.in_tokens,
            "out": self.out_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "tokens": self.in_tokens + self.out_tokens,
            "usd": round(self.usd, 6),
            "has_unpriced": self.has_unpriced,
        }


@dataclass(slots=True)
class Rollups:
    """All the aggregates the UI needs, plus the live-session bucket when requested."""

    totals: dict[str, Bucket] = field(default_factory=dict)
    daily: list[dict[str, Any]] = field(default_factory=list)
    by_model: list[dict[str, Any]] = field(default_factory=list)
    by_provider: list[dict[str, Any]] = field(default_factory=list)
    by_workspace: list[dict[str, Any]] = field(default_factory=list)
    session: Bucket | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for transport/rendering."""
        return {
            "totals": {k: v.to_dict() for k, v in self.totals.items()},
            "daily": self.daily,
            "by_model": self.by_model,
            "by_provider": self.by_provider,
            "by_workspace": self.by_workspace,
            "session": self.session.to_dict() if self.session is not None else None,
        }


def _local_date(ts: str) -> Any:
    """The local calendar date of a stored UTC ``ts`` (``…Z``); None if unparseable."""
    try:
        parsed = datetime.fromisoformat(ts)  # 3.11 accepts a trailing 'Z'
    except ValueError:
        return None
    return parsed.astimezone().date()


def _grouped(entries: list[UsageEntry], key: Any) -> list[dict[str, Any]]:
    """Bucket ``entries`` by ``key(entry)``, sorted by spend then turns, desc."""
    buckets: dict[str, Bucket] = {}
    for e in entries:
        buckets.setdefault(key(e), Bucket()).add(e)
    rows = [{"key": k, **b.to_dict()} for k, b in buckets.items()]
    rows.sort(key=lambda r: (r["usd"], r["turns"]), reverse=True)
    return rows


def summarize(
    entries: list[UsageEntry], *, now: datetime, session_id: str | None = None
) -> Rollups:
    """Aggregate ``entries`` into today/7d/30d/all-time totals, a daily series, and groupings."""
    today = now.astimezone().date()
    d7 = today - timedelta(days=6)
    d30 = today - timedelta(days=29)

    totals = {k: Bucket() for k in ("today", "last_7d", "last_30d", "all_time")}
    per_day: dict[Any, Bucket] = {}
    session = Bucket() if session_id is not None else None

    for e in entries:
        totals["all_time"].add(e)
        day = _local_date(e.ts)
        if day is None:
            continue
        if day >= d30:
            totals["last_30d"].add(e)
            per_day.setdefault(day, Bucket()).add(e)
        if day >= d7:
            totals["last_7d"].add(e)
        if day == today:
            totals["today"].add(e)
        if session is not None and e.session_id == session_id:
            session.add(e)

    daily: list[dict[str, Any]] = []
    for i in range(_DAILY_DAYS):
        day = today - timedelta(days=_DAILY_DAYS - 1 - i)
        b = per_day.get(day, Bucket())
        daily.append(
            {
                "date": day.isoformat(),
                "usd": round(b.usd, 6),
                "tokens": b.in_tokens + b.out_tokens,
                "turns": b.turns,
                "has_unpriced": b.has_unpriced,
            }
        )

    return Rollups(
        totals=totals,
        daily=daily,
        by_model=_grouped(entries, lambda e: e.model),
        by_provider=_grouped(entries, lambda e: e.provider),
        by_workspace=_grouped(entries, lambda e: e.workspace),
        session=session,
    )
