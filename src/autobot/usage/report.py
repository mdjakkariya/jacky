"""A self-contained, on-device HTML usage dashboard.

Renders the ``Rollups.to_dict()`` shape to a single HTML string with **inline** CSS and
**inline SVG** charts — no JavaScript, no CDN, no web fonts, no external requests of any
kind (the on-device-only constraint). Written to a local file and opened in the user's own
browser — never the hosted/Artifact path.
"""

from __future__ import annotations

import html
import webbrowser
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem; font: 15px/1.5 -apple-system, system-ui, sans-serif;
       background: #fbfbfd; color: #1d1d1f; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
.sub { color: #6e6e73; margin: 0 0 1.5rem; font-size: .85rem; }
.cards { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
.card { background: #fff; border: 1px solid #e5e5ea; border-radius: 12px; padding: 1rem 1.25rem;
        min-width: 160px; }
.card .label { color: #6e6e73; font-size: .75rem; text-transform: uppercase;
               letter-spacing: .04em; }
.card .value { font-size: 1.6rem; font-weight: 600; margin-top: .25rem; }
.card .meta { color: #6e6e73; font-size: .8rem; margin-top: .25rem; }
section { background: #fff; border: 1px solid #e5e5ea; border-radius: 12px; padding: 1.25rem;
          margin-bottom: 1.25rem; }
h2 { font-size: 1rem; margin: 0 0 1rem; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #f0f0f2; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.bar { fill: #0a84ff; }
.axis { fill: #6e6e73; font-size: 10px; }
.empty { color: #6e6e73; text-align: center; padding: 3rem 0; }
@media (prefers-color-scheme: dark) {
  body { background: #000; color: #f5f5f7; }
  .card, section { background: #1c1c1e; border-color: #2c2c2e; }
  th, td { border-color: #2c2c2e; }
}
"""


def _money(usd: float, has_unpriced: bool = False) -> str:
    prefix = "≥ " if has_unpriced else ""
    return f"{prefix}${usd:,.2f}"


def _card(label: str, bucket: dict[str, Any]) -> str:
    cache = int(bucket.get("cache_read", 0)) + int(bucket.get("cache_write", 0))
    return (
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{_money(bucket["usd"], bucket["has_unpriced"])}</div>'
        f'<div class="meta">{bucket["turns"]} turns · {bucket["tokens"]:,} tokens'
        f" · {cache:,} cache</div></div>"
    )


def _daily_svg(daily: list[dict[str, Any]]) -> str:
    if not daily:
        return ""
    width, height, pad = 720, 160, 24
    peak = max((d["usd"] for d in daily), default=0.0) or 1.0
    n = len(daily)
    slot = (width - 2 * pad) / n
    bars = []
    for i, d in enumerate(daily):
        bh = (d["usd"] / peak) * (height - 2 * pad)
        x = pad + i * slot
        y = height - pad - bh
        bars.append(
            f'<rect class="bar" x="{x:.1f}" y="{y:.1f}" width="{max(slot - 2, 1):.1f}" '
            f'height="{bh:.1f}"><title>{html.escape(d["date"])}: '
            f"{_money(d['usd'])}</title></rect>"
        )
    first, last = html.escape(daily[0]["date"]), html.escape(daily[-1]["date"])
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="daily cost">{"".join(bars)}'
        f'<text class="axis" x="{pad}" y="{height - 6}">{first}</text>'
        f'<text class="axis" x="{width - pad}" y="{height - 6}" text-anchor="end">{last}</text>'
        f"</svg>"
    )


def _group_table(title: str, rows: list[dict[str, Any]], key_label: str) -> str:
    if not rows:
        return ""
    body = "".join(
        f"<tr><td>{html.escape(str(r['key']))}</td>"
        f'<td class="num">{r["turns"]}</td>'
        f'<td class="num">{r["tokens"]:,}</td>'
        f'<td class="num">{int(r.get("cache_read", 0)) + int(r.get("cache_write", 0)):,}</td>'
        f'<td class="num">{_money(r["usd"], r["has_unpriced"])}</td></tr>'
        for r in rows[:12]
    )
    return (
        f"<section><h2>{html.escape(title)}</h2><table><thead><tr>"
        f'<th>{html.escape(key_label)}</th><th class="num">turns</th>'
        f'<th class="num">tokens</th><th class="num">cache</th>'
        f'<th class="num">cost</th></tr></thead>'
        f"<tbody>{body}</tbody></table></section>"
    )


def build_html(rollups: dict[str, Any], *, now: datetime) -> str:
    """Render the rollups dict to a self-contained HTML page (no external requests)."""
    totals = rollups.get("totals", {})
    all_time = totals.get("all_time", {})
    stamp = now.astimezone().strftime("%Y-%m-%d %H:%M")
    if not all_time or all_time.get("turns", 0) == 0:
        body = '<div class="empty">No usage recorded yet.</div>'
    else:
        cards = "".join(
            _card(label, totals[key])
            for label, key in (
                ("Today", "today"),
                ("Last 7 days", "last_7d"),
                ("All time", "all_time"),
            )
            if key in totals
        )
        chart = _daily_svg(rollups.get("daily", []))
        body = (
            f'<div class="cards">{cards}</div>'
            + (f"<section><h2>Daily cost (30 days)</h2>{chart}</section>" if chart else "")
            + _group_table("Cost by model", rollups.get("by_model", []), "model")
            + _group_table("Cost by workspace", rollups.get("by_workspace", []), "workspace")
            + _group_table("Cost by provider", rollups.get("by_provider", []), "provider")
        )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Jack usage</title><style>{_CSS}</style></head><body>"
        "<h1>Jack — usage &amp; cost</h1>"
        f'<p class="sub">Local estimate from recorded token counts (cache read+write '
        f"included in cost). List prices — no promo assumed, so your actual bill may be "
        f"lower; the provider console is authoritative. Local turns are $0. "
        f"Generated {html.escape(stamp)}.</p>"
        f"{body}</body></html>"
    )


def write_and_open(
    rollups: dict[str, Any],
    *,
    now: datetime,
    dest: Path | None = None,
    open_browser: Callable[[str], bool] = webbrowser.open,
) -> Path:
    """Write the report to ``dest`` (default ``~/.autobot/usage-report.html``) and open it."""
    target = dest or (Path.home() / ".autobot" / "usage-report.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_html(rollups, now=now), encoding="utf-8")
    open_browser(target.as_uri())
    return target
