"""R21 — prompt efficacy tracking.

The compounding loop: dreamy reads sessions, dreamy's prompts get pasted into
sessions, so dreamy can detect its own artifacts in later transcripts and
observe whether the work then completed.

The distinction this module exists to protect: **absence of a marker is
`not_observed`, never `failed`.** A prompt that was never pasted has told us
nothing about its quality, and collapsing that into a failure count would
quietly manufacture a metric out of missing data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Emitted by prompt_compiler into every artifact footer:
#   <!-- dreamy-prompt:v1:<hash> skills=... chain: ... -->
MARKER_RE = re.compile(r"dreamy-prompt:v(\d+):([0-9a-f]{6,})")

# Bounded vocabulary. `not_observed` is a first-class state, not a failure.
OBSERVED = "observed"
NOT_OBSERVED = "not_observed"


@dataclass
class ArtifactEfficacy:
    artifact_type: str
    total: int = 0
    observed: int = 0
    not_observed: int = 0
    harnesses: dict[str, int] = field(default_factory=dict)
    completed_after_use: int = 0
    unresolved_after_use: int = 0

    @property
    def observation_rate(self) -> float | None:
        """Share of artifacts seen in a later transcript, or None when there
        is nothing to divide by — never a silent 0.0, which would read as a
        measured failure."""
        return round(self.observed / self.total, 4) if self.total else None


def extract_markers(text: str) -> list[str]:
    """Every dreamy artifact marker hash present in a blob of transcript."""
    return [m.group(2) for m in MARKER_RE.finditer(text or "")]


def _artifact_markers(store) -> dict[str, tuple]:
    """marker-hash -> (artifact_type, project_id) for every emitted artifact.

    Deliberately UNFILTERED on archived_ms (D24/D31): efficacy measures the
    compounding loop over everything ever emitted — an archived artifact was
    still served at emission time and its marker appearing in a later
    transcript is real observation evidence. Filtering to served-only would
    shrink the denominator to the current window and corrupt the rate.
    """
    out: dict[str, tuple] = {}
    rows = store.conn.execute(
        "SELECT project_id, prompt_type, content FROM prompt_artifacts"
    ).fetchall()
    for row in rows:
        for marker in extract_markers(row["content"] or ""):
            out[marker] = (row["prompt_type"], row["project_id"])
    return out


def _observed_markers(store) -> dict[str, set]:
    """marker-hash -> set of source_ids whose turns contain that marker.

    Turn excerpts are redacted and truncated at ingestion, so this scans the
    fingerprint and raw_meta columns that survive that boundary.
    """
    found: dict[str, set] = {}
    rows = store.conn.execute(
        "SELECT source_id, content_fingerprint, raw_meta_json FROM turns "
        "WHERE content_fingerprint LIKE '%dreamy-prompt%' "
        "   OR raw_meta_json LIKE '%dreamy-prompt%'"
    ).fetchall()
    for row in rows:
        blob = f"{row['content_fingerprint'] or ''} {row['raw_meta_json'] or ''}"
        for marker in extract_markers(blob):
            found.setdefault(marker, set()).add(row["source_id"])
    return found


def compute(store) -> dict[str, ArtifactEfficacy]:
    """Per-artifact-type efficacy over the current state DB."""
    emitted = _artifact_markers(store)
    seen = _observed_markers(store)

    by_type: dict[str, ArtifactEfficacy] = {}
    for marker, (artifact_type, project_id) in emitted.items():
        entry = by_type.setdefault(artifact_type, ArtifactEfficacy(artifact_type=artifact_type))
        entry.total += 1
        sources = seen.get(marker)
        if not sources:
            entry.not_observed += 1
            continue
        entry.observed += 1
        for source_id in sources:
            entry.harnesses[source_id] = entry.harnesses.get(source_id, 0) + 1

        # Only artifacts we actually observed in use can say anything about
        # subsequent outcomes. Everything else stays out of the numerator AND
        # the denominator.
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM intent_episodes "
            "WHERE project_id=? AND completion_status IN ('unverified','complete')",
            (project_id,),
        ).fetchone()
        if row and int(row["n"] or 0) > 0:
            entry.completed_after_use += 1
        else:
            entry.unresolved_after_use += 1
    return by_type


def report(store) -> dict:
    """Machine-readable efficacy summary."""
    data = compute(store)
    return {
        "artifact_types": {
            name: {
                "total": e.total,
                OBSERVED: e.observed,
                NOT_OBSERVED: e.not_observed,
                "observation_rate": e.observation_rate,
                "harnesses": dict(sorted(e.harnesses.items())),
                "completed_after_use": e.completed_after_use,
                "unresolved_after_use": e.unresolved_after_use,
            }
            for name, e in sorted(data.items())
        },
        "note": (
            "not_observed means the artifact was never seen in a later transcript — "
            "no evidence either way. It is never counted as a failure."
        ),
    }
