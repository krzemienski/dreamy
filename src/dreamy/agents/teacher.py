"""Teacher agent — explain debt patterns with persistent hash retention.

Persistent retention: signature_hash = sha256(category|evidence_json|title).
We persist each explanation as an agent_event with topic='teacher' and store
the signature_hash in fields_json. A second run with the same signature
short-circuits — no second model call.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import AgentContext

TEACHER_SCHEMA = {
    "type": "object",
    "properties": {
        "explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "signature_hash": {"type": "string"},
                    "explanation_md": {"type": "string"},
                    "category": {"type": "string"},
                    "severity": {"type": "string"},
                },
                "required": ["finding_id", "signature_hash", "explanation_md"],
            },
        },
    },
    "required": ["explanations"],
}


@dataclass
class TeacherBlock:
    explanations: list[dict[str, Any]] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0


def _signature(finding: dict[str, Any]) -> str:
    payload = (
        f"{finding.get('category','')}|"
        f"{json.dumps(finding.get('evidence', {}), sort_keys=True)}|"
        f"{finding.get('title','')}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _lookup_cached(store, signature: str) -> str | None:
    """Find a previous explanation matching this signature in agent_events."""

    if store is None:
        return None
    row = store.conn.execute(
        "SELECT message FROM agent_events WHERE topic='teacher' AND fields_json LIKE ? ORDER BY ts_ms DESC LIMIT 1",
        (f"%{signature}%",),
    ).fetchone()
    if row is None:
        return None
    return row["message"]


def _enabled(ctx: AgentContext) -> bool:
    return bool(ctx.cfg.agents_enabled.get("teacher", True))


def run(ctx: AgentContext) -> TeacherBlock:
    if not _enabled(ctx):
        ctx.log.info("teacher skipped", agent_type="teacher", reason="disabled")
        return TeacherBlock()

    if getattr(ctx, "skip_reason", None) is not None:
        ctx.log.info(
            "teacher skipped: project-level skip",
            agent_type="teacher",
            project_id=ctx.project_id,
            reason=ctx.skip_reason,
        )
        return TeacherBlock()

    ctx.log.info("teacher started", agent_type="teacher", project_id=ctx.project_id)

    findings = ctx.evidence.get("findings", []) or []
    explanations: list[dict[str, Any]] = []
    hits = misses = 0
    misses_to_call: list[dict[str, Any]] = []

    for f in findings:
        sig = _signature(f)
        cached = _lookup_cached(ctx.store, sig)
        if cached:
            explanations.append({
                "finding_id": f.get("id"),
                "signature_hash": sig,
                "explanation_md": cached,
                "category": f.get("category", ""),
                "severity": f.get("severity", ""),
            })
            hits += 1
        else:
            misses_to_call.append({**f, "_signature": sig})
            misses += 1

    if misses_to_call:
        from .. import agent_sdk

        prompt = (
            "For each finding below, explain WHY the debt exists, what design "
            "decision produced it, and the smallest safe fix. Do NOT propose "
            "edits to the running repo. Output structured explanations.\n\n"
            f"Findings:\n{json.dumps(misses_to_call, indent=2, default=str)}"
        )
        result = agent_sdk.call_claude(
            prompt=prompt,
            schema=TEACHER_SCHEMA,
            agent_type="teacher",
            run_id=ctx.run_id,
            store=ctx.store,
            cfg=ctx.cfg,
        )
        if result.structured_output and not result.error_text:
            new = (result.structured_output or {}).get("explanations", []) or []
            persisted = 0
            for entry in new:
                explanations.append(entry)
                if ctx.store is not None:
                    ctx.store.insert_agent_event(
                        run_id=ctx.run_id,
                        agent_type="teacher",
                        call_id=None,
                        ts_ms=ctx.evidence.get("now_ms", 0) or 0,
                        topic="teacher",
                        level="INFO",
                        message=entry.get("explanation_md", ""),
                        fields_json=json.dumps({
                            "signature_hash": entry.get("signature_hash", ""),
                            "finding_id": entry.get("finding_id", ""),
                        }),
                    )
                    # Also persist as an `agent` finding so the
                    # explanation surfaces in the same table as every
                    # other finding and satisfies SPEC R10/T3.
                    try:
                        ctx.store.insert_finding_with_provenance(
                            ctx.project_id,
                            {
                                "category": "debt_explanation",
                                "severity": entry.get("severity", "medium") or "medium",
                                "title": f"debt_explanation[{entry.get('finding_id','')}]",
                                "detail": entry.get("explanation_md", "")[:200],
                                "evidence": {
                                    "signature_hash": entry.get("signature_hash", ""),
                                    "category": entry.get("category", ""),
                                    "finding_id": entry.get("finding_id", ""),
                                },
                                "confidence": "medium",
                            },
                            provenance="agent",
                            run_id=ctx.run_id,
                        )
                        persisted += 1
                    except ValueError:
                        pass
            ctx.log.info(
                "teacher persisted explanations",
                agent_type="teacher",
                persisted_count=persisted,
            )
        elif result.error_text:
            # Missing / failed calls NEVER fabricate explanations.
            ctx.log.info(
                "teacher no-op",
                agent_type="teacher",
                project_id=ctx.project_id,
                error_text=result.error_text,
                stop_reason=result.stop_reason,
            )

    block = TeacherBlock(explanations=explanations, cache_hits=hits, cache_misses=misses)
    ctx.log.info(
        "teacher produced explanations",
        agent_type="teacher",
        project_id=ctx.project_id,
        count=len(explanations),
        cache_hits=hits,
        cache_misses=misses,
    )
    return block
