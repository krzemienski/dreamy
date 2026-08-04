"""Ingest phase — discover + scan all connectors into the Store.

Runs each registered connector's discover()/scan() cycle, advances
per-source watermarks, and persists any records that map directly onto
canonical tables (currently: router_request -> router_requests). Session
and turn assembly from other record types is owned by the correlate phase,
which consumes the raw SourceRecord list returned via IngestStats.records.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import Config
from .connectors import make_connectors
from .logging_util import get_logger
from .protocol import SourceRecord
from .store import Store

log = get_logger("ingest")


def _iso_ms(ms: int) -> str:
    """UTC ISO-8601 for a watermark, so the warning names a real instant."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds")


@dataclass
class IngestStats:
    source_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    records: list[SourceRecord] = field(default_factory=list)
    # I2: source_id -> "ok" | "drifted" | "absent". A drifted source still
    # scans; the outcome is reported, never enforced.
    format_health: dict[str, str] = field(default_factory=dict)


def ingest(
    store: Store,
    cfg: Config,
    lookback_days: int | None = None,
    source_path_overrides: dict[str, str] | None = None,
    keep_records: bool = True,
) -> IngestStats:
    """Discover and scan every connector in ALL_CONNECTORS, writing router
    requests directly into the store and advancing watermarks. Returns
    IngestStats with per-source counts, warnings, and (optionally) the raw
    records for downstream correlate/analyze phases.
    """
    stats = IngestStats()
    effective_lookback = lookback_days if lookback_days is not None else cfg.lookback_days
    overrides = source_path_overrides or {}
    configured_overrides = cfg.source_paths or {}

    for connector in make_connectors():
        source_id = connector.SOURCE_ID
        override = overrides.get(source_id) or configured_overrides.get(source_id)

        # I2: evaluate format health BEFORE the availability gate. A drifted
        # SQLite source can present as unavailable (its probe query targets a
        # table that moved), so recording health only for available sources
        # would drop the very case that matters most.
        try:
            health = connector.format_health(override)
            stats.format_health[source_id] = health.outcome
            if health.outcome == "drifted":
                msg = (
                    f"{source_id}: SOURCE FORMAT DRIFT — {health.detail}"
                    + (f" (sampled {health.sampled_file})" if health.sampled_file else "")
                )
                stats.warnings.append(msg)
                log.warn(
                    "source format drifted", source_id=source_id,
                    detail=health.detail, sampled_file=health.sampled_file,
                )
        except Exception as exc:  # noqa: BLE001
            # A canary that crashes must not take the run down with it: its
            # whole purpose is to report, and a failed report is strictly less
            # bad than a failed ingest.
            #
            # But it is recorded as `error`, NOT `absent`. `absent` means "this
            # harness is not installed here", a normal state every surface
            # renders quietly — so labelling a crash `absent` would file the
            # check's own failure under the one outcome nobody looks at, and
            # the canary would silently stop guarding while still reporting.
            stats.format_health[source_id] = "error"
            msg = f"{source_id}: format health check FAILED — {type(exc).__name__}: {exc}"
            stats.warnings.append(msg)
            log.warn("format health check failed", source_id=source_id, error=str(exc))

        info = connector.discover(override)
        if not info.available:
            msg = f"{source_id}: unavailable ({info.error or 'not found'})"
            stats.warnings.append(msg)
            log.warn("source unavailable", source_id=source_id, path=info.path, error=info.error)
            continue

        wm = store.get_watermark(source_id)

        # R11.a: an EXPLICIT `--lookback-days` that reaches back beyond the
        # watermark is a backfill request, and must not be clamped by it.
        #
        # Every connector floors its scan at `ts <= watermark_ms`, so passing
        # the live watermark makes `--lookback-days 90` a silent no-op once a
        # 30d run has advanced it: the operator asks for 90 days, the store
        # returns the same rows, and nothing reports that the window was
        # ignored. Measured on this machine before the fix: watermarks sat at
        # 2026-08-04 while the oldest stored session was 2026-06-01, so no
        # explicit window could ever reach the gap.
        #
        # Dropping the floor is safe because dedup is STRUCTURAL, not
        # watermark-derived: `sessions`/`router_requests` carry
        # UNIQUE(source_id, native_id) and `turns` a content-addressed id, all
        # written via INSERT OR IGNORE. Verified against the live store —
        # 7,712 sessions, 272,873 turns, 120,570 router requests, 0 duplicate
        # groups (evidence 43-idempotency-probe.txt). Re-scanning costs time,
        # never correctness.
        #
        # Scope is deliberately narrow. The scheduled 6h run passes
        # lookback_days=None and keeps the incremental floor, which is the
        # whole point of a watermark: bounded work per run. Only an operator
        # naming a window explicitly opts into the full re-scan.
        scan_floor = wm
        backfill = False
        if lookback_days is not None:
            window_start_ms = int(
                (datetime.now(UTC) - timedelta(days=effective_lookback)).timestamp() * 1000
            )
            if window_start_ms < wm:
                scan_floor = 0
                backfill = True
                log.info(
                    "explicit lookback predates watermark; scanning full window",
                    source_id=source_id,
                    watermark_ms=wm,
                    window_start_ms=window_start_ms,
                    lookback_days=effective_lookback,
                )

        count = 0
        max_ts = wm
        # I9: this run's own scan timestamps, kept for cadence. Persisted
        # history alone is NOT enough — correlate writes `turns` AFTER ingest,
        # so a first ingest of a session source sees empty history. Merged
        # with the store below.
        scanned_ts: list[int] = []

        for rec in connector.scan(scan_floor, effective_lookback, override):
            count += 1
            scanned_ts.append(rec.timestamp_ms)
            if rec.timestamp_ms > max_ts:
                max_ts = rec.timestamp_ms

            if keep_records:
                stats.records.append(rec)

            if rec.record_type == "router_request":
                meta = rec.raw_meta or {}
                store.insert_router(
                    source_id=rec.source_id,
                    native_id=rec.native_id,
                    ts_ms=rec.timestamp_ms,
                    provider=meta.get("provider") or "",
                    model=rec.model or "",
                    conn_id=meta.get("connection_id") or "",
                    status=meta.get("status") or "",
                    cost=meta.get("cost_usd"),
                    ptoks=meta.get("prompt_tokens"),
                    ctoks=meta.get("completion_tokens"),
                    endpoint=meta.get("endpoint") or "",
                )
            # Session/turn assembly for other record types is deferred to
            # the correlate phase, which has project resolution available.

        if count and max_ts > wm:
            store.set_watermark(source_id, max_ts)

        # I9: cadence from merged history, NOT either source alone.
        #
        # Persisted history alone is empty on a FIRST ingest of a session
        # source (correlate writes `turns` after this phase). The current scan
        # batch alone is a one-record sample on incremental runs, which would
        # overwrite a valid 200-record baseline with `insufficient`. Both
        # failure modes erase the staleness detector's input exactly when it
        # is needed, so the baseline is the union: durable history plus this
        # run's own scan, deduplicated, newest N.
        if count:
            from . import cadence as _cadence

            try:
                merged = sorted(
                    set(scanned_ts) | set(store.cadence_timestamps(source_id, _cadence.N)),
                    reverse=True,
                )[: _cadence.N]
                _cadence.refresh(store, source_id, merged)
            except Exception as exc:  # noqa: BLE001 — surfaced below, never silent
                # Cadence is advisory — it must not fail the run — but an
                # invisible failure is worse than none: a broken detector that
                # keeps reporting green trains the reader to trust it. Put it
                # in `stats.warnings`, which reaches the operator, alongside
                # the log line, which reaches nobody on an unattended run.
                stats.warnings.append(
                    f"{source_id}: cadence refresh failed ({type(exc).__name__}: {exc})"
                )
                log.warn("cadence refresh failed", source_id=source_id, exc_info=True)

        stats.source_counts[source_id] = count
        if count == 0:
            # A zero count has two distinct causes and the operator must be
            # able to tell them apart. Reporting both as "no records within
            # the window" is a false negative: it reads as "this source has
            # gone dark" while the true state is often "everything in the
            # window was already ingested on an earlier run", because scan()
            # starts at the watermark, not at the window floor.
            #
            # Observed live 2026-08-04: a run reported `claude: no records
            # within 30d window` while the store held 5,989 claude sessions
            # whose newest was 90 minutes old. Same defect class as the web
            # source-state labelling fix — label by observation, never by
            # inference.
            # A backfill scanned the whole window with no watermark floor, so
            # "caught up" is the wrong label even though a watermark exists:
            # nothing was skipped, the window genuinely holds no records.
            if backfill:
                msg = (
                    f"{source_id}: no records in {effective_lookback}d backfill window "
                    f"(path {info.path}, full re-scan, watermark floor ignored)"
                )
                reason = "empty_backfill"
            elif wm > 0:
                msg = (
                    f"{source_id}: no NEW records since watermark "
                    f"{_iso_ms(wm)} (window {effective_lookback}d, path {info.path})"
                )
                reason = "caught_up"
            else:
                msg = (
                    f"{source_id}: no records within {effective_lookback}d window "
                    f"(path {info.path}, never ingested)"
                )
                reason = "empty_window"
            stats.warnings.append(msg)
            log.warn(
                "source contributed nothing", source_id=source_id, reason=reason,
                path=info.path, lookback_days=effective_lookback, watermark_ms=wm,
            )
        log.info("scanned source", source_id=source_id, count=count, watermark_ms=max_ts)

    store.commit()
    return stats
