"""Findings diff (R13) — classify each finding by stable ID."""
from __future__ import annotations

from .read import FindingDiff, ReadStore


def findings_diff(
    read_store: ReadStore,
    curr_run_id: str | None = None,
) -> list[FindingDiff]:
    """Pure function on the store. Deterministic for a given state.

    The predecessor run is resolved from the observation ledger per project,
    so it is not a caller-supplied argument — passing one let two callers
    disagree about what "previous" meant for the same finding.
    """

    return read_store.findings_diff(curr_run_id)
