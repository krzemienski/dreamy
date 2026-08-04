"""Compiler agent — orchestrates prompt artifact emission via prompt_compiler."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import AgentContext


@dataclass
class CompilerBlock:
    emitted: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


def _enabled(ctx: AgentContext) -> bool:
    return bool(ctx.cfg.agents_enabled.get("compiler", True))


def _slug(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]


def _emit_artifact(
    artifact_type: str,
    project: dict[str, Any],
    evidence: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    from .. import evidence_projection, prompt_compiler

    # ctx.evidence is the full analysis payload (episodes, findings, sessions,
    # git, seeds); `evidence` is whatever the caller passed for this artifact.
    # Merge so a caller-supplied override wins but nothing is silently lost.
    merged: dict[str, Any] = dict(ctx.evidence or {})
    merged.update(evidence or {})

    project_slug = _slug(project.get("path", project.get("id", "")))
    # Project raw analysis output into the flat per-section shape the renderers
    # read. Without this the artifacts render literal "(none)" sections and
    # "HEAD unknown" — structurally valid, operationally worthless.
    rendered_evidence = evidence_projection.build(
        project=project,
        analysis_evidence=merged,
        output_dir=Path(ctx.output_dir),
        project_slug=project_slug,
    )
    content, stable_hash, footer = prompt_compiler.emit_artifact(
        artifact_type=artifact_type,
        project=project,
        evidence=rendered_evidence,
        cfg=ctx.cfg,
        output_dir=ctx.output_dir,
        project_slug=project_slug,
    )
    out_dir = Path(ctx.output_dir) / "reports" / "latest" / "projects" / project_slug / "prompts"
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / f"{artifact_type}.md"
    artifact_path.write_text(content, encoding="utf-8")
    if ctx.store is not None:
        # Row identity is (project, artifact_type) — NOT the content hash.
        # Including the hash minted a fresh row on every content change, so the
        # table accumulated one row per historical version and "latest artifact"
        # queries became ambiguous.
        ctx.store.conn.execute(
            "INSERT INTO prompt_artifacts(id, project_id, prompt_type, stable_hash, content, created_ms, "
            "archived_ms, chain_json, health_json) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "stable_hash=excluded.stable_hash, content=excluded.content, "
            "chain_json=excluded.chain_json, health_json=excluded.health_json",
            (
                hashlib.sha256(f"{artifact_type}:{project.get('id','')}".encode()).hexdigest()[:16],
                project.get("id", ""),
                artifact_type,
                stable_hash,
                content,
                ctx.evidence.get("now_ms", 0) or 0,
                __import__("json").dumps(footer, sort_keys=True),
                __import__("json").dumps(
                    {
                        "nonempty": bool(content.strip()),
                        "portable": not prompt_compiler.detect_unresolved_skill_refs(content),
                        "sections": content.count("\n## "),
                        "bytes": len(content),
                    },
                    sort_keys=True,
                ),
            ),
        )
        ctx.store.commit()
    ctx.log.info(
        "compiler emit",
        agent_type="compiler",
        artifact_type=artifact_type,
        project_id=project.get("id", ""),
        path=str(artifact_path),
        stable_hash=stable_hash[:8],
    )
    return {"artifact_type": artifact_type, "stable_hash": stable_hash, "output_path": str(artifact_path)}


def run(ctx: AgentContext) -> CompilerBlock:
    if not _enabled(ctx):
        ctx.log.info("compiler skipped", agent_type="compiler", reason="disabled")
        return CompilerBlock()

    ctx.log.info("compiler started", agent_type="compiler", project_id=ctx.project_id)

    block = CompilerBlock()
    project = ctx.evidence.get("project", {"id": ctx.project_id, "path": ctx.project_path})
    # All four artifact types emit unconditionally (TASK.md VG-4 capture:
    # "All four artifacts per project"). Conditional emission was silent
    # coverage loss — the D15/D24 class. Empty inputs render as explicit
    # degraded-but-valid bodies (the renderers' defaults and '- (none)'
    # fallbacks), so a project with nothing unverified still yields a
    # validation artifact that says exactly that.
    for artifact_type in ("resumption", "validation", "remediation", "next_tasks"):
        try:
            emitted = _emit_artifact(artifact_type, project, ctx.evidence, ctx)
            if emitted:
                block.emitted.append(emitted)
        except Exception as exc:  # noqa: BLE001
            ctx.log.warn(
                "compiler emit failed",
                agent_type="compiler",
                artifact_type=artifact_type,
                error=str(exc),
            )
            block.skipped.append({
                "reason": "emit_error",
                "project_id": project.get("id"),
                "artifact_type": artifact_type,
                "error": str(exc),
            })

    return block
