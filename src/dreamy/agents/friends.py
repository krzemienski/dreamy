"""Friends-network agent — same-machine cross-session synthesis.

Three tiers: same_project (tier 1) → same_intent (tier 2) → same_time (tier 3).
Scope: same-machine store only. No network, no git remote enum.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import AgentContext


@dataclass
class FriendsBlock:
    neighbors: list[dict[str, Any]] = field(default_factory=list)
    insights: list[str] = field(default_factory=list)


_WINDOW_MS = 24 * 3600 * 1000


def _normalize_fingerprint(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _enabled(ctx: AgentContext) -> bool:
    return bool(ctx.cfg.agents_enabled.get("friends", True))


def _tier1_same_project(ctx: AgentContext) -> list[dict[str, Any]]:
    if ctx.store is None:
        return []
    rows = ctx.store.conn.execute(
        "SELECT source_id, native_id, started_ms, ended_ms, model, project_path "
        "FROM sessions WHERE project_id=? AND source_id != 'nine_router' "
        "ORDER BY started_ms DESC LIMIT 50",
        (ctx.project_id,),
    ).fetchall()
    return [
        {
            "source_id": r["source_id"],
            "native_id": r["native_id"],
            "started_ms": r["started_ms"],
            "model": r["model"],
            "linked_via": "same_project",
            "confidence": "high",
        }
        for r in rows
    ]


def _tier2_same_intent(ctx: AgentContext) -> list[dict[str, Any]]:
    if ctx.store is None:
        return []
    rows = ctx.store.conn.execute(
        "SELECT source_id, native_id, started_ms, model, content_fingerprint, project_path "
        "FROM turns WHERE project_id != ? AND project_id IS NOT NULL AND content_fingerprint IS NOT NULL "
        "ORDER BY started_ms DESC LIMIT 200",
        (ctx.project_id,),
    ).fetchall()
    target_fp = _normalize_fingerprint(
        ctx.evidence.get("original_intent", "")
    )
    if not target_fp:
        return []
    matches = []
    for r in rows:
        fp = _normalize_fingerprint(r["content_fingerprint"] or "")
        if fp and (target_fp in fp or fp in target_fp):
            matches.append({
                "source_id": r["source_id"],
                "native_id": r["native_id"],
                "started_ms": r["started_ms"],
                "model": r["model"],
                "linked_via": "same_intent",
                "confidence": "med",
            })
            if len(matches) >= 10:
                break
    return matches


def _tier3_same_time(ctx: AgentContext) -> list[dict[str, Any]]:
    if ctx.store is None:
        return []
    anchor = ctx.evidence.get("anchor_ms", int(time.time() * 1000))
    rows = ctx.store.conn.execute(
        "SELECT source_id, native_id, started_ms, model, project_path FROM sessions "
        "WHERE started_ms BETWEEN ? AND ? AND project_id != ? AND project_id IS NOT NULL "
        "ORDER BY started_ms DESC LIMIT 20",
        (anchor - _WINDOW_MS, anchor + _WINDOW_MS, ctx.project_id),
    ).fetchall()
    return [
        {
            "source_id": r["source_id"],
            "native_id": r["native_id"],
            "started_ms": r["started_ms"],
            "model": r["model"],
            "linked_via": "same_time",
            "confidence": "low",
        }
        for r in rows
    ]


def run(ctx: AgentContext) -> FriendsBlock:
    if not _enabled(ctx):
        ctx.log.info("friends skipped", agent_type="friends", reason="disabled")
        return FriendsBlock()

    if getattr(ctx, "skip_reason", None) is not None:
        ctx.log.info(
            "friends skipped: project-level skip",
            agent_type="friends",
            project_id=ctx.project_id,
            reason=ctx.skip_reason,
        )
        return FriendsBlock()

    ctx.log.info("friends started", agent_type="friends", project_id=ctx.project_id)

    neighbors: list[dict[str, Any]] = []
    seen = set()
    for tier_fn in (_tier1_same_project, _tier2_same_intent, _tier3_same_time):
        for nb in tier_fn(ctx):
            key = (nb["source_id"], nb["native_id"])
            if key in seen:
                continue
            seen.add(key)
            neighbors.append(nb)
            if len(neighbors) >= 25:
                break
        if len(neighbors) >= 25:
            break

    # Insights come from a short model call summarizing observed neighbors.
    insights: list[str] = []
    if neighbors and len(neighbors) >= 2:
        from .. import agent_sdk

        prompt = (
            "Synthesize 3-7 short language insights about HOW the following "
            "neighboring sessions approached similar work. No plans, no edits. "
            "Each insight one sentence.\n\n"
            f"Neighbors:\n{neighbors}"
        )
        result = agent_sdk.call_claude(
            prompt=prompt,
            schema={
                "type": "object",
                "properties": {"insights": {"type": "array", "items": {"type": "string"}}},
            },
            agent_type="friends",
            run_id=ctx.run_id,
            store=ctx.store,
            cfg=ctx.cfg,
        )
        if result.structured_output and not result.error_text:
            insights = (result.structured_output or {}).get("insights", []) or []
            # Persist each non-empty insight as an `agent` finding and
            # tag it with the neighbor count so the operator can see why.
            if ctx.store is not None:
                persisted = 0
                for s in insights:
                    text = (s or "").strip()
                    if not text:
                        continue
                    try:
                        ctx.store.insert_finding_with_provenance(
                            ctx.project_id,
                            {
                                "category": "cross_session_insight",
                                "severity": "low",
                                "title": text[:120],
                                "detail": text,
                                "evidence": {"neighbor_count": len(neighbors)},
                                "confidence": "medium",
                            },
                            provenance="agent",
                            run_id=ctx.run_id,
                        )
                        persisted += 1
                    except ValueError:
                        pass
                ctx.log.info(
                    "friends persisted insights",
                    agent_type="friends",
                    persisted_count=persisted,
                )
        elif result.error_text:
            ctx.log.info(
                "friends no-op",
                agent_type="friends",
                project_id=ctx.project_id,
                error_text=result.error_text,
                stop_reason=result.stop_reason,
            )

    block = FriendsBlock(neighbors=neighbors, insights=insights)
    ctx.log.info(
        "friends produced neighbors",
        agent_type="friends",
        project_id=ctx.project_id,
        neighbor_count=len(neighbors),
        insight_count=len(insights),
    )
    return block
