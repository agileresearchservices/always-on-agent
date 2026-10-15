"""`python -m aoa usage`: estimated API spend per day from the `usage` table.

Covers the live services only. Evals use throwaway data dirs, so their spend is not here. This is an estimate from
list prices (see `pricing.py`); the Console's Usage and Cost page has the billed amounts.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .pricing import cost_usd
from .store import Store


def kind_of(conv_id: str) -> str:
    if conv_id.startswith("prreview/"):
        return "pr-review"
    if "/helper" in conv_id:
        return "helper"
    return "agent"


def collect(store: Store, since: float, cache_ttl: int) -> list[dict[str, Any]]:
    with store._conn() as c:
        rows = c.execute(
            "SELECT conv_id, ts, model, input_tokens i, output_tokens o, cache_read r, cache_write w"
            " FROM usage WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    out = []
    for r in rows:
        cost = cost_usd(r["model"], r["i"] or 0, r["o"] or 0, r["r"] or 0, r["w"] or 0, cache_ttl)
        out.append({
            "day": datetime.fromtimestamp(r["ts"]).date().isoformat(), "kind": kind_of(r["conv_id"]),
            "model": r["model"], "calls": 1, "out": r["o"] or 0, "cost": cost,
        })
    return out


def _sum(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "out": 0, "cost": 0.0, "unpriced": 0})
    for r in rows:
        a = agg[r[key]]
        a["calls"] += 1
        a["out"] += r["out"]
        if r["cost"] is None:
            a["unpriced"] += 1
        else:
            a["cost"] += r["cost"]
    return dict(sorted(agg.items()))


def _table(title: str, agg: dict[str, dict[str, Any]]) -> list[str]:
    lines = [title, f"  {'':<12}{'calls':>7}{'output tok':>12}{'est. cost':>12}"]
    for k, a in agg.items():
        note = f"  ({a['unpriced']} calls unpriced)" if a["unpriced"] else ""
        lines.append(f"  {k:<12}{a['calls']:>7}{a['out']:>12,}{'$' + format(a['cost'], '.2f'):>12}{note}")
    return lines


def render(rows: list[dict[str, Any]], days: int) -> str:
    if not rows:
        return f"No usage recorded in the last {days} day(s)."
    by_day, by_kind = _sum(rows, "day"), _sum(rows, "kind")
    total = sum(a["cost"] for a in by_day.values())
    avg = total / len(by_day)
    lines = _table("By day (local time)", by_day) + [""] + _table("By source, same window", by_kind)
    lines += ["", f"Total ${total:.2f} over {len(by_day)} day(s) with usage, average ${avg:.2f}/day.",
              "Estimate from list prices; evals are not included. Billed amounts: Console > Usage and Cost."]
    return "\n".join(lines)


def run(days: int = 2) -> str:
    settings = Settings.from_env()
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    store = Store(settings.db_path)
    return render(collect(store, start.timestamp(), settings.cache_ttl_seconds), days)
