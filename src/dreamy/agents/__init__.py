"""Agent base + per-agent modules.

Four agent types per the spec: research (find/extract), teacher (explain
debt patterns with persistent retention), friends-network (same-machine
cross-session synthesis), compiler (prompt artifact orchestration).

Each agent runs verbose topic logs via logging_util. agents_enabled flags
in config gate optional off-switching; spend cap is enforced inside the SDK
wrapper, never per-agent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentContext:
    project_id: str
    project_path: str
    project_path_slug: str
    cfg: Any  # Config
    output_dir: Path
    run_id: str | None
    store: Any  # Store
    log: Any  # TopicLogger
    evidence: dict[str, Any] = field(default_factory=dict)
    # Set by `run.run_pipeline` when this project's paid agents are
    # skipped (no cap configured, or the run's spend cap is already
     # exhausted before this project was reached). Paid agents that
     # observe a non-None `skip_reason` MUST exit fast without any
     # model call and MUST NOT persist fabricated findings.
    skip_reason: str | None = None
