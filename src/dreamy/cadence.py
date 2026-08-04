"""I9 — connector staleness detector.

Per-source cadence is DERIVED, not configured: the median inter-record gap
over the last N ingested records for that source. A source whose watermark
is older than k × that median is stale. Both constants are named so they
can be argued with; they are not knobs for operators.

Computed at ingest (or any caller that holds the timestamps) and stored in
`source_cadence`. Read surfaces re-evaluate staleness against the live
watermark and wall clock — a stored `stale` flag would rot between runs.

SQL for the cadence table lives here, not on Store: the migration owns the
schema, this module owns the read/write of the rows.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import Store

# Last N timestamps feed the median. Large enough that one burst cannot
# halve it; small enough to track a harness whose rhythm genuinely changed.
N = 200
# Stale only after missing roughly eight of its own typical intervals.
# False alarms train operators to ignore the detector; a day late is cheaper.
K = 8
# Below this floor a median is a guess from too few gaps. Two records give
# one gap; one unusual day would define the baseline forever.
MIN_SAMPLE = 20

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_INSUFFICIENT = "insufficient data"


@dataclass(frozen=True)
class CadenceAssessment:
    """Result of one compute-or-read evaluation for a single source."""

    source_id: str
    sample_size: int
    median_gap_ms: int | None
    computed_ms: int
    status: str  # STATUS_OK | STATUS_STALE | STATUS_INSUFFICIENT
    stale: bool

    @property
    def cadence(self) -> str:
        """Surface label used by status / SourceStat (`cadence: …`)."""
        return self.status


def median_gap_ms(
    timestamps_ms: Sequence[int],
    *,
    n: int = N,
    min_sample: int = MIN_SAMPLE,
) -> tuple[int | None, int]:
    """Median consecutive gap over the most recent ``n`` timestamps.

    Returns ``(median_gap_ms | None, sample_size)``. ``median_gap_ms`` is
    None when the sample is below ``min_sample`` or when every gap is
    non-positive (identical timestamps) — both are "no meaningful cadence",
    never a license to call the source stale.
    """
    cleaned = sorted(int(t) for t in timestamps_ms)
    if not cleaned:
        return None, 0
    window = cleaned[-n:] if len(cleaned) > n else cleaned
    sample_size = len(window)
    if sample_size < min_sample:
        return None, sample_size
    gaps = [window[i] - window[i - 1] for i in range(1, len(window))]
    # A zero or negative median means the "interval" is not a real period —
    # k×0 would mark every source stale the instant the clock ticks. Refuse
    # rather than invent a threshold.
    gap = int(median(gaps))
    if gap <= 0:
        return None, sample_size
    return gap, sample_size


def assess(
    source_id: str,
    *,
    watermark_ms: int,
    median_gap_ms: int | None,
    sample_size: int,
    computed_ms: int,
    now_ms: int | None = None,
    k: int = K,
    min_sample: int = MIN_SAMPLE,
) -> CadenceAssessment:
    """Classify a source from a stored (or just-computed) cadence row.

    Staleness is evaluated HERE, not stored: it is a function of the live
    watermark and the current clock. A row written at ingest that said
    "ok" would lie an hour later if the harness went dark.
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    if sample_size < min_sample or median_gap_ms is None or median_gap_ms <= 0:
        return CadenceAssessment(
            source_id=source_id,
            sample_size=sample_size,
            median_gap_ms=None if sample_size < min_sample else median_gap_ms,
            computed_ms=computed_ms,
            status=STATUS_INSUFFICIENT,
            stale=False,
        )
    age = now - int(watermark_ms or 0)
    stale = age > int(k) * int(median_gap_ms)
    return CadenceAssessment(
        source_id=source_id,
        sample_size=sample_size,
        median_gap_ms=int(median_gap_ms),
        computed_ms=computed_ms,
        status=STATUS_STALE if stale else STATUS_OK,
        stale=stale,
    )


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "source_id": row["source_id"],
        "median_gap_ms": row["median_gap_ms"],
        "sample_size": int(row["sample_size"] or 0),
        "computed_ms": int(row["computed_ms"] or 0),
    }


def upsert_source_cadence(
    store: Store,
    source_id: str,
    *,
    median_gap_ms: int | None,
    sample_size: int,
    computed_ms: int,
) -> None:
    """Persist one source's cadence baseline into ``source_cadence``."""
    store.conn.execute(
        "INSERT INTO source_cadence(source_id, median_gap_ms, sample_size, computed_ms) "
        "VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
        "median_gap_ms=excluded.median_gap_ms, sample_size=excluded.sample_size, "
        "computed_ms=excluded.computed_ms",
        (source_id, median_gap_ms, int(sample_size), int(computed_ms)),
    )


def get_source_cadence(store: Store, source_id: str) -> dict[str, Any] | None:
    """Return the stored cadence row for ``source_id``, or None."""
    row = store.conn.execute(
        "SELECT source_id, median_gap_ms, sample_size, computed_ms "
        "FROM source_cadence WHERE source_id=?",
        (source_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_dict(row)


def all_source_cadence(store: Store) -> dict[str, dict[str, Any]]:
    """All stored cadence rows, keyed by source_id."""
    rows = store.conn.execute(
        "SELECT source_id, median_gap_ms, sample_size, computed_ms FROM source_cadence"
    ).fetchall()
    return {row["source_id"]: _row_dict(row) for row in rows}


def refresh(
    store: Store,
    source_id: str,
    timestamps_ms: Sequence[int],
    *,
    now_ms: int | None = None,
    n: int = N,
    k: int = K,
    min_sample: int = MIN_SAMPLE,
) -> CadenceAssessment:
    """Compute cadence from timestamps, persist it, return the assessment.

    Call site is ingest (or a test seeding the same path): the records are
    already in hand there. This module does not scan connectors.
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    gap, sample = median_gap_ms(timestamps_ms, n=n, min_sample=min_sample)
    upsert_source_cadence(
        store,
        source_id,
        median_gap_ms=gap,
        sample_size=sample,
        computed_ms=now,
    )
    return assess(
        source_id,
        watermark_ms=store.get_watermark(source_id),
        median_gap_ms=gap,
        sample_size=sample,
        computed_ms=now,
        now_ms=now,
        k=k,
        min_sample=min_sample,
    )


def assess_stored(
    store: Store,
    source_id: str,
    *,
    now_ms: int | None = None,
    k: int = K,
    min_sample: int = MIN_SAMPLE,
) -> CadenceAssessment | None:
    """Re-evaluate a previously persisted cadence row. None if never computed."""
    row = get_source_cadence(store, source_id)
    if row is None:
        return None
    return assess(
        source_id,
        watermark_ms=store.get_watermark(source_id),
        median_gap_ms=row["median_gap_ms"],
        sample_size=int(row["sample_size"] or 0),
        computed_ms=int(row["computed_ms"] or 0),
        now_ms=now_ms,
        k=k,
        min_sample=min_sample,
    )


def assess_row(
    source_id: str,
    row: Mapping[str, Any],
    *,
    watermark_ms: int,
    now_ms: int | None = None,
    k: int = K,
    min_sample: int = MIN_SAMPLE,
) -> CadenceAssessment:
    """Assess a cadence row dict already loaded by the caller (e.g. source_stats)."""
    return assess(
        source_id,
        watermark_ms=watermark_ms,
        median_gap_ms=row.get("median_gap_ms"),
        sample_size=int(row.get("sample_size") or 0),
        computed_ms=int(row.get("computed_ms") or 0),
        now_ms=now_ms,
        k=k,
        min_sample=min_sample,
    )
