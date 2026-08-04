"""Cost attribution (R10) + helpers."""
from __future__ import annotations

import statistics

from .read import CostByProject, ReadStore


def cost_per_project(read_store: ReadStore, since_ms: int) -> list[CostByProject]:
    rows = read_store._store.conn.execute(
        "SELECT id FROM projects ORDER BY last_seen_ms DESC"
    ).fetchall()
    out: list[CostByProject] = []
    for row in rows:
        c = read_store.cost_30d(row["id"])
        if c is not None:
            out.append(c)
    return out


def cost_summary(read_store: ReadStore, since_ms: int) -> dict:
    per_project = cost_per_project(read_store, since_ms)
    totals = [c.total_usd for c in per_project]
    return {
        "per_project": [
            {"project_id": c.project_id, "total_usd": c.total_usd, "per_intent_usd": c.per_completed_intent_usd}
            for c in per_project
        ],
        "total_usd": sum(totals),
        "p50_usd": statistics.median(totals) if totals else 0.0,
        "p90_usd": (statistics.quantiles(totals, n=10)[8] if len(totals) >= 10 else max(totals or [0.0])),
        "project_count": len(per_project),
    }
