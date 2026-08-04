"""Research agent — find/extract per finding or theme."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import AgentContext

# Schema for research findings — every field the `findings` table needs
# is required, so a schema-valid paid output can be persisted directly
# via `Store.insert_finding_with_provenance(..., provenance='agent')`
# without us fabricating or rewriting any field. Required fields
# cover SPEC R10/T3 (provenance is set by the persist call, not by the
# model; the model produces the content that the contract demands).
RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "evidence": {"type": "object"},
                    "support_session_ids": {"type": "array", "items": {"type": "string"}},
                    "source_session_ids": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["low", "med", "high"]},
                },
                "required": ["category", "severity", "title", "detail", "evidence", "confidence"],
            },
        },
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "session_ids": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                },
            },
        },
    },
    "required": ["findings"],
}


@dataclass
class ResearchBlock:
    findings: list[dict[str, Any]] = field(default_factory=list)
    themes: list[dict[str, Any]] = field(default_factory=list)


def _enabled(ctx: AgentContext) -> bool:
    return bool(ctx.cfg.agents_enabled.get("research", True))


def run(ctx: AgentContext) -> ResearchBlock:
    if not _enabled(ctx):
        ctx.log.info("research skipped", agent_type="research", reason="disabled")
        return ResearchBlock()

    if getattr(ctx, "skip_reason", None) is not None:
        # Per-project skip from run.run_pipeline. No model call, no
        # fabricated findings. The skip reason is propagated through
        # result.skipped_projects by run.py already.
        ctx.log.info(
            "research skipped: project-level skip",
            agent_type="research",
            project_id=ctx.project_id,
            reason=ctx.skip_reason,
        )
        return ResearchBlock()

    ctx.log.info("research started", agent_type="research", project_id=ctx.project_id)

    from .. import agent_sdk

    prompt = (
        "Analyze the following project evidence and produce supporting citations "
        "for each finding plus any cross-session themes. Cite session IDs and "
        "file paths when possible.\n\n"
        f"Project: {ctx.project_path}\n"
        f"Project ID: {ctx.project_id}\n\n"
        f"Evidence:\n{ctx.evidence}"
    )
    result = agent_sdk.call_claude(
        prompt=prompt,
        schema=RESEARCH_SCHEMA,
        agent_type="research",
        run_id=ctx.run_id,
        store=ctx.store,
        cfg=ctx.cfg,
    )
    block = ResearchBlock()
    if result.error_text or not result.structured_output:
        # Missing / unavailable / schema-invalid agent calls NEVER
        # fabricate findings. Persist nothing.
        ctx.log.info(
            "research no-op",
            agent_type="research",
            project_id=ctx.project_id,
            error_text=result.error_text,
            stop_reason=result.stop_reason,
        )
        return block
    block.findings = result.structured_output.get("findings", []) or []
    block.themes = result.structured_output.get("themes", []) or []

    # Persist ONLY what the model returned AND the schema validated.
    # Schema-validity is the agent_sdk wrapper's job; here we trust the
    # single field contract enforced by Store.insert_finding_with_provenance
    # and persist each `findings[*]` entry verbatim (mapped onto the
    # `findings` table column contract). No fabrication, no rewrite.
    persisted = 0
    if ctx.store is not None and block.findings:
        for entry in block.findings:
            try:
                ctx.store.insert_finding_with_provenance(
                    ctx.project_id,
                    {
                        "category": entry.get("category", "research_support"),
                        "severity": entry.get("severity", "medium"),
                        "title": entry.get("title", ""),
                        "detail": entry.get("detail", ""),
                        "evidence": {
                            **dict(entry.get("evidence", {})),
                            "support_session_ids": entry.get("support_session_ids", []),
                            "source_session_ids": entry.get("source_session_ids", []),
                        },
                        "confidence": entry.get("confidence", "medium"),
                    },
                    provenance="agent",
                    run_id=ctx.run_id,
                )
                persisted += 1
            except ValueError:
                # provenance validation failure is impossible here; the
                # function only allows 'deterministic'/'agent' and we
                # pass 'agent'. Defensive swallow for the future.
                pass
    ctx.log.info(
        "research extracted findings",
        agent_type="research",
        project_id=ctx.project_id,
        finding_count=len(block.findings),
        theme_count=len(block.themes),
        agent_findings_persisted=persisted,
        cost_usd=result.cost_usd,
    )
    return block
