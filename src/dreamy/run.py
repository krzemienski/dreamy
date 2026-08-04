"""Pipeline orchestrator — single entrypoint that wires the full Phase 2→7 flow.

Sequence: configure_logging → open store → start_run → ingest → correlate
→ per-project analyze + agent layer → compiler → reports → finish_run.
Never raises; records warnings instead so the run completes unless the
launchd lock is held.
"""
from __future__ import annotations

import os
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import correlate, ingest, logging_util
from . import store as store_mod
from .orchestration import ResultEnvelope, UnitOutput, map_units
from .read import ReadStore

if TYPE_CHECKING:
    from .analyze import AnalysisResult
    from .changes import GitEvidence
    from .correlate import SessionBucket


@dataclass(frozen=True)
class _ProjectAnalysisUnit:
    project_path: str
    sessions: tuple[SessionBucket, ...]
    turns: tuple[Any, ...]
    since_ms: int


@dataclass(frozen=True)
class _ProjectAnalysisResult:
    analysis: AnalysisResult
    git_evidence: GitEvidence | None
    # The evidence as GATHERED, including a failed read. `git_evidence` is
    # None-ed out when it carries an error so the analyzer never treats an
    # empty snapshot as "clean tree", but the failure itself must still be
    # persisted — otherwise `project_git_snapshots.error` can never be
    # populated and a bundle cannot distinguish "no run captured this" from
    # "the run tried and git refused".
    git_capture: GitEvidence | None = None


def _analyze_project_unit(unit: _ProjectAnalysisUnit) -> UnitOutput[_ProjectAnalysisResult]:
    from . import analyze as analyze_mod
    from . import changes as changes_mod

    warnings: list[str] = []
    git_evidence = None
    git_capture = None
    try:
        git_capture = changes_mod.gather_git_evidence(unit.project_path, unit.since_ms)
        git_evidence = git_capture
        if git_capture.error:
            warnings.append(
                f"git evidence unavailable for {unit.project_path}: {git_capture.error}"
            )
            git_evidence = None
    except Exception as exc:  # noqa: BLE001 — analysis continues without git evidence
        warnings.append(f"git evidence failed for {unit.project_path}: {exc}")
        git_capture = changes_mod.GitEvidence(error=str(exc))

    analysis = analyze_mod.analyze_project(
        project_path=unit.project_path,
        sessions=list(unit.sessions),
        turns_excerpts=unit.turns,
        git_evidence=git_evidence,
    )
    return UnitOutput(
        result=_ProjectAnalysisResult(
            analysis=analysis,
            git_evidence=git_evidence,
            git_capture=git_capture,
        ),
        warnings=tuple(warnings),
    )


@dataclass
class RunResult:
    run_id: str
    started_ms: int
    ended_ms: int = 0
    status: str = "running"
    source_counts: dict[str, int] = field(default_factory=dict)
    # I2: source_id -> "ok" | "drifted" | "absent", so an operator reading a
    # run result can tell "this source is quiet" from "this source's on-disk
    # format moved and we may be silently parsing nothing".
    format_health: dict[str, str] = field(default_factory=dict)
    finding_count: int = 0
    prompt_count: int = 0
    agent_cost_usd: float = 0.0
    error_count: int = 0
    warning_count: int = 0
    # Warning TEXT, not just the count: a bare count tells an operator that
    # something was skipped but never what or why (TASK.md:202).
    warnings: list[str] = field(default_factory=list)
    # VG-3 fields. Every paid-agent invocation leaves an audit row; every
    # project skipped AFTER a paid call completed is recorded here so the
    # operator can tell cap-deterministic blocks from cap-overrun blocks.
    # Heterogeneous by design: reason is str, remaining_usd/max_call_usd are
    # float, cap_usd and max_call_usd may be None. Callers only serialise it.
    skipped_projects: list[dict[str, Any]] = field(default_factory=list)
    agent_call_records: list[dict[str, Any]] = field(default_factory=list)
    agent_findings_persisted: int = 0
    # Status of paid-agent analysis for this run. Mirrors `agent_calls` so
    # the report can tell apart "no paid calls were enabled" from "cap hit".
    agent_run_status: str = "disabled"  # disabled | partial | completed | capped


def _persist_episodes(store, project_id: str, episodes: list[dict[str, Any]]):
    import json

    for ep in episodes:
        eid = store_mod.stable_id("episode", project_id, str(ep.get("started_ms", 0)), ep.get("original_intent", ""))
        # completion_evidence is a JSON-encoded snapshot of the deterministic
        # classifier inputs/decision-support at the moment of classification —
        # without it VG-2 cannot independently review WHY a completion status
        # was assigned, because the DB row would carry only the status label
        # with no proof back to classifier inputs (commit count, error excerpts,
        # days_since_last_activity, completion_claim_matched).
        ev = ep.get("completion_evidence")
        if ev is None:
            ev_json = None
        elif isinstance(ev, str):
            ev_json = ev
        else:
            ev_json = json.dumps(ev, sort_keys=True, default=str)
        store.conn.execute(
            "INSERT OR REPLACE INTO intent_episodes(id, project_id, started_ms, ended_ms, original_intent, "
            "completion_status, completion_evidence_json, drift_type, pivot_point_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                eid,
                project_id,
                ep.get("started_ms", 0),
                ep.get("ended_ms"),
                ep.get("original_intent", ""),
                ep.get("completion_status", "unverified"),
                ev_json,
                ep.get("drift_type"),
                ep.get("pivot_point_ms"),
            ),
        )


def _persist_findings(
    store, project_id: str, findings: list[dict[str, Any]], run_id: str | None = None
):
    import json

    for f in findings:
        fid = store_mod.stable_id("finding", project_id, f.get("title", ""), f.get("category", ""))
        evidence_json = json.dumps(f.get("evidence", {}))
        # Upsert, never INSERT OR REPLACE: REPLACE deletes the prior row, which
        # would clear resolved_ms/dismissal_reason and resurrect every dismissed
        # finding on the next run (R20 requires a dismissal to stick).
        #
        # `state` is deliberately NOT written from here any more. It was written
        # as the literal 'new' on every upsert, so every row in a real database
        # read `new` forever and R13's four-state classification was decorative.
        # State is now DERIVED at read time from `finding_observations`, which
        # cannot go stale the way a written-once string does.
        store.conn.execute(
            "INSERT INTO findings(id, project_id, category, severity, title, detail, "
            "evidence_json, confidence, created_ms, resolved_ms, provenance, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "severity=excluded.severity, detail=excluded.detail, "
            "evidence_json=excluded.evidence_json, confidence=excluded.confidence, "
            "provenance=excluded.provenance",
            (
                fid,
                project_id,
                f.get("category", "debt"),
                f.get("severity", "medium"),
                f.get("title", ""),
                f.get("detail", ""),
                evidence_json,
                f.get("confidence", "medium"),
                f.get("created_ms", int(time.time() * 1000)),
                f.get("provenance", "deterministic") or "deterministic",
                f.get("state", "new"),
            ),
        )
        _observe_finding(store, fid, run_id, evidence_json)


def _observe_finding(store, fid: str, run_id: str | None, evidence_json: str) -> None:
    """Record this run's sighting and apply the R20 material-change rule.

    A dismissed finding whose evidence still hashes the same is the same
    judgement the operator already made — it stays dismissed. One whose
    evidence changed is a different claim wearing the same id, so the
    dismissal is lifted and R13 will classify it `regressed`.
    """
    if not run_id:
        return
    now = int(time.time() * 1000)
    row = store.conn.execute(
        "SELECT dismissal_reason, dismissed_evidence_sha FROM findings WHERE id=?", (fid,)
    ).fetchone()
    if row is not None and row["dismissal_reason"]:
        current_sha = store_mod.finding_evidence_sha(evidence_json)
        if row["dismissed_evidence_sha"] and current_sha != row["dismissed_evidence_sha"]:
            store.conn.execute(
                "UPDATE findings SET resolved_ms=NULL, dismissal_reason=NULL, "
                "dismissed_evidence_sha=NULL, dismissed_ms=NULL WHERE id=?",
                (fid,),
            )
        else:
            # Still dismissed: observed this run, but recorded as resolved so
            # the diff never re-surfaces it as new work.
            store.record_finding_observation(fid, run_id, now, resolved=True)
            return
    store.record_finding_observation(fid, run_id, now, resolved=False)


def _persist_agent_finding(
    store, project_id: str, f: dict[str, Any], run_id: str | None = None
) -> None:
    """Persist a single model-derived finding into `findings` with
    `provenance='agent'` enforced at write time. The companion to
    `_persist_findings` (deterministic provenance). Agent findings MUST
    NOT be written without going through here — that is the guarantee
    that `provenance` is non-null and exactly 'agent' for paid work (R10/T3).
    Missing/failed/unavailable agent calls leave this table untouched."""
    import json
    evidence_json = json.dumps(f.get("evidence", {}))
    fid = store_mod.stable_id(
        "finding",
        project_id,
        f.get("title", ""),
        f.get("category", "research_support"),
        str(f.get("confidence", "")),
        json.dumps(f.get("evidence", {}), sort_keys=True, default=str),
    )
    store.conn.execute(
        "INSERT INTO findings(id, project_id, category, severity, title, detail, "
        "evidence_json, confidence, created_ms, resolved_ms, provenance, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'agent', ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "severity=excluded.severity, detail=excluded.detail, "
        "evidence_json=excluded.evidence_json, confidence=excluded.confidence",
        (
            fid,
            project_id,
            f.get("category", "research_support"),
            f.get("severity", "medium"),
            f.get("title", ""),
            f.get("detail", ""),
            evidence_json,
            f.get("confidence", "medium"),
            int(time.time() * 1000),
            f.get("state", "new"),
        ),
    )
    _observe_finding(store, fid, run_id, evidence_json)


def _mark_agent_skipped_legacy(
    error_text: str,
    project_id: str,
    project_path: str,
    result: RunResult,
) -> None:
    """Recorded once when `SpendCapExceeded` is caught defensively from
    a paid agent call. We never re-raise and never increment error_count
    for cap-driven skips — they are designed behavior, not failures."""
    result.skipped_projects.append({
        "project_id": project_id,
        "project_path": project_path,
        "reason": "agent_skipped_spend_cap",
        "detail": error_text,
    })
    if result.agent_run_status not in ("capped",):
        result.agent_run_status = "capped"


def _stale_placeholder_content(artifact_type: str) -> str:
    """Fallback body written when a stale artifact file cannot be unlinked
    (e.g. permission error). Deliberately fails EVERY coldstart check for
    the artifact_type it can (missing required sections for resumption; no
    `## Detected platform` for validation) and never carries a `compiler=`
    footer, so `stale_compiler_stamp` is always True regardless of
    artifact_type. A served stale prompt must never look like a working
    one — this is a decisive failure marker, not a best-effort note.
    """
    return (
        f"# {artifact_type.upper()} — STALE, ARCHIVED\n\n"
        "## STALE ARTIFACT — DO NOT USE\n\n"
        "This artifact was invalidated by D24 compiler-version reconciliation "
        "and could not be removed from disk (unlink failed). Its prior content "
        "has been replaced with this placeholder so it fails every cold-start "
        "check rather than being served as current output. Re-run the "
        "pipeline; if this persists, check filesystem permissions on "
        "reports/latest/.\n"
    )


def _reconcile_compiler_stamps(s, output_dir: Path, cfg, log, run_id: str) -> dict[str, int]:
    """D24: settle every `prompt_artifacts` row against TODAY's compiler.

    Called twice per pipeline run:
      1. At run start (right after the lock is acquired, before ingest) —
         archives+unlinks every row already stale on disk from a PRIOR run,
         before this run's compile loop even begins. This is the "invalidate
         at run start" requirement: a cold operator reading `latest/`
         mid-run, or a run that errors before reaching the compile loop,
         never sees pre-existing stale bytes.
      2. After the compile loop — rows the loop just recompiled now carry a
         fresh `compiler=<stamp>` in their content and get un-archived;
         any row STILL mismatched (inactive project, phantom path, or the
         loop skipped it) is archived+unlinked here.

    A row's own footer carries `compiler=<stamp>` (prompt_compiler.
    current_compiler_stamp). If a row's stamp does not match what TODAY's
    compiler would produce for that artifact_type — including a row with
    no stamp at all (pre-v4) — the row was emitted by different code than
    what is on disk right now (acceptance/VG-4/D24-stale-live-tree.md,
    "Root cause, third pass"):
      - stamp matches -> `compiler_version` set, `archived_ms` cleared.
      - stamp mismatches (or cannot be computed) -> `archived_ms` set to
        now (if not already). The on-disk file is UNLINKED so
        `reports/latest/` can never continue serving the stale bytes it
        already contains. If unlink itself fails (e.g. permission error),
        the file is overwritten with `_stale_placeholder_content` instead
        — never left with its original stale bytes. If BOTH unlink and
        overwrite fail for any row, this function raises, so the caller
        can fail the run rather than silently proceed with a stale file
        still servable on disk.

    FAIL CLOSED, not fail open. This function does NOT catch-and-continue
    on unexpected errors the way most of this pipeline does. On any
    exception during the main pass, it attempts one fallback: archive
    every row unconditionally and unlink/overwrite every prompt file under
    `reports/latest/projects/*/prompts/*.md` with no stamp comparison at
    all — "we could not verify anything, so nothing is servable as fresh."
    Only if that fallback ALSO fails does this function re-raise, which
    propagates out of `run_pipeline`'s outer try/except and marks the
    whole run `status="failed"` — because at that point neither DB state
    nor disk state can be trusted to not be serving stale content, and a
    silently-completed run with the coldstart gate's whole purpose
    (D24) potentially defeated is worse than a visibly failed run.
    """
    import hashlib as _hashlib
    import re as _re

    from . import prompt_compiler as _prompt_compiler

    def _fail_closed_target(path: Path, artifact_type: str) -> None:
        try:
            if path.exists():
                path.unlink()
            return
        except OSError:
            pass
        # Unlink failed — overwrite with the placeholder instead of leaving
        # the original stale content readable. Atomic write to avoid a
        # partial file if this itself is interrupted.
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(_stale_placeholder_content(artifact_type), encoding="utf-8")
        os.replace(temp, path)

    archived_count = 0
    fresh_count = 0
    try:
        stamp_rows = s.conn.execute(
            "SELECT pa.id, pa.prompt_type, pa.content, pa.archived_ms, p.path "
            "FROM prompt_artifacts pa JOIN projects p ON p.id=pa.project_id"
        ).fetchall()
        now_ms_val = int(time.time() * 1000)
        for srow in stamp_rows:
            m = _re.search(r"compiler=([0-9a-f]+)", srow["content"] or "")
            found_stamp = m.group(1) if m else None
            try:
                expected_stamp = _prompt_compiler.current_compiler_stamp(srow["prompt_type"], cfg)
            except Exception:  # noqa: BLE001
                expected_stamp = None
            slug = _hashlib.sha256(srow["path"].encode("utf-8")).hexdigest()[:12]
            target = output_dir / "reports" / "latest" / "projects" / slug / "prompts" / f"{srow['prompt_type']}.md"
            if expected_stamp is not None and found_stamp == expected_stamp:
                s.conn.execute(
                    "UPDATE prompt_artifacts SET compiler_version=?, archived_ms=NULL WHERE id=?",
                    (expected_stamp, srow["id"]),
                )
                fresh_count += 1
            else:
                if srow["archived_ms"] is None:
                    s.conn.execute(
                        "UPDATE prompt_artifacts SET archived_ms=? WHERE id=?",
                        (now_ms_val, srow["id"]),
                    )
                archived_count += 1
                # Deliberately NOT wrapped in a swallow-all except: a failure
                # here (both unlink and overwrite failing) means this row's
                # stale bytes remain servable, and that must abort the whole
                # pass — see the fail-closed fallback below.
                _fail_closed_target(target, srow["prompt_type"])
        s.commit()
        log.info(
            "stale artifact invalidation done",
            run_id=run_id,
            archived=archived_count,
            fresh=fresh_count,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "stale artifact invalidation failed — falling back to archive-all",
            run_id=run_id,
            error=str(exc),
        )
        try:
            s.conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        try:
            now_ms_val = int(time.time() * 1000)
            s.conn.execute(
                "UPDATE prompt_artifacts SET archived_ms=? WHERE archived_ms IS NULL",
                (now_ms_val,),
            )
            s.commit()
            fallback_count = 0
            for md_path in sorted((output_dir / "reports" / "latest" / "projects").glob("*/prompts/*.md")):
                _fail_closed_target(md_path, md_path.stem)
                fallback_count += 1
            log.error(
                "stale artifact invalidation: archive-all fallback succeeded",
                run_id=run_id,
                archived_all_rows=True,
                unlinked_or_overwritten=fallback_count,
            )
            return {"archived": fallback_count, "fresh": 0}
        except Exception as fallback_exc:  # noqa: BLE001
            log.error(
                "stale artifact invalidation: archive-all fallback ALSO failed — aborting run",
                run_id=run_id,
                error=str(fallback_exc),
            )
            raise
    return {"archived": archived_count, "fresh": fresh_count}



def run_pipeline(
    cfg,
    output_dir: Path,
    run_id: str | None = None,
    lookback_days: int | None = None,
    source_path_overrides: dict[str, str] | None = None,
) -> RunResult:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    run_id = run_id or f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    logging_util.configure_logging(output_dir=output_dir, run_id=run_id, level=cfg.log_level)
    log = logging_util.get_logger("run")
    # One resolution of the window, used for the log line, the persisted
    # `runs.lookback_days` (R11.b) and the per-window report path (R11.c),
    # so all three can never disagree about which window this run covered.
    effective_lookback = lookback_days if lookback_days is not None else cfg.lookback_days
    log.info("pipeline started", run_id=run_id, lookback_days=effective_lookback)

    db_path = output_dir / "state.db"
    s = store_mod.Store(db_path)
    result = RunResult(run_id=run_id, started_ms=int(time.time() * 1000))
    lock = None  # bound before try so `finally` is safe on early failure

    try:
        # Acquire single-instance lock
        from .store import RunLock

        lock_path = output_dir / "lock"
        lock = RunLock(lock_path)
        if not lock.acquire():
            log.error("run lock held", run_id=run_id)
            result.status = "lock_held"
            result.ended_ms = int(time.time() * 1000)
            return result

        # Reconcile orphans BEFORE opening this run's row. Order matters twice
        # over: the lock is held (so any surviving `running` row is provably
        # abandoned, not a live sibling), and our own row does not exist yet
        # (so the blanket predicate cannot close the run we are about to
        # start). Reversing either would be a self-inflicted bug.
        recovered = s.recover_interrupted_runs(result.started_ms)

        # Record the run only once the lock is held, and inside the try so a
        # failure here still runs the finally block that closes the connection.
        s.start_run(run_id, result.started_ms, lookback_days=effective_lookback)
        # Commit immediately, NOT swallowed: `_reconcile_compiler_stamps`
        # below may issue a ROLLBACK on its own failure path, which would
        # otherwise also discard this uncommitted `runs` row (sqlite3's
        # implicit transaction spans every write since the last commit).
        # If this commit itself fails, the DB is in a state this function
        # cannot reason about safely — propagate to the outer except, which
        # marks the whole run `status="failed"` rather than continuing with
        # run bookkeeping that may not exist.
        s.commit()
        if recovered:
            # Logged AFTER the commit, never before: a log line claiming rows
            # were recovered while the transaction could still roll back would
            # assert a state change that never reached disk. The recovery
            # UPDATE and this run's `runs` row commit together, so the log is
            # only emitted once both are durable.
            log.warn("recovered interrupted runs", run_id=run_id, count=recovered)


        # D24, phase A: invalidate at RUN START, before ingest/compile even
        # begin. Any row already stale from a PRIOR run (or never touched
        # since a compiler fix landed) is archived+unlinked here, so
        # `reports/latest/` never serves pre-existing stale bytes even if
        # this run errors out before reaching the compile loop.
        _reconcile_compiler_stamps(s, output_dir, cfg, log, run_id)

        # 1. Ingest
        stats = ingest.ingest(s, cfg, lookback_days=lookback_days, source_path_overrides=source_path_overrides)
        result.source_counts = dict(stats.source_counts)
        result.format_health = dict(stats.format_health)
        result.warning_count = len(stats.warnings)
        result.warnings = list(stats.warnings)
        log.info("ingest done", run_id=run_id, source_counts=result.source_counts)

        # 2. Correlate
        # Pass records through to correlate — for now use the records the ingest
        # pipeline kept in memory (keep_records=True by default).
        # The window is config-driven (DESIGN.md §3.3, "N default 30s,
        # configurable"). Passing nothing here silently made the function
        # default the shipping value and left the documented key inert.
        window_ms = int(cfg.correlation_window_seconds) * 1000
        corr = correlate.correlate_records(stats.records, window_ms=window_ms)
        log.info("correlate done", run_id=run_id, session_count=len(corr.sessions),
                 router_links=len(corr.router_links), window_ms=window_ms)

        # 2b. Persist correlated projects/sessions/turns (Phase 1 canonical tables)
        persisted = correlate.persist_correlated(s, stats.records, corr)
        log.info(
            "persist done",
            run_id=run_id,
            projects=persisted.projects,
            sessions=persisted.sessions,
            turns=persisted.turns,
        )

        # I3: cumulative spend pre-flight, BEFORE any paid agent call this
        # run. `spend_cap_usd` bounds ONE run; nothing summed spend ACROSS
        # runs, so a positive per-run cap composes into an unbounded
        # cumulative total at N scheduled runs/day. Checked here, before the
        # per-project loop below issues a single paid call, because a cap
        # checked only after spending already happened is not a cap.
        cumulative_cap_reason: str | None = None
        now_for_caps = int(time.time() * 1000)

        # ADMISSION CONTROL, not budget narrowing. A run is admitted only when
        # its FULL configured per-run cap fits inside the remaining cumulative
        # headroom; otherwise the entire agent layer is skipped.
        #
        # Two weaker designs were tried and rejected:
        #
        #   1. `spent >= cap` — checks the PRIOR total, so $9 spent against a
        #      $10 daily cap still admits a run with a $5 per-run cap: $14.
        #      The ceiling is enforced exactly one run too late.
        #   2. Narrow the per-run budget to the remaining headroom. Better, but
        #      still not a ceiling: `call_claude`'s pre-call gate can only prove
        #      a call would overshoot when a max-observed per-call cost already
        #      exists for the run. On the FIRST paid call there is no history,
        #      the gate reduces to `remaining > 0`, and one call can land above
        #      the headroom. Anthropic bills actual token usage; there is no
        #      request-level dollar limit to enforce against.
        #
        # Admission control needs no per-call estimate: if the worst case the
        # per-run cap already permits still fits under the ceiling, the ceiling
        # holds no matter what a single call costs. The price is utilisation —
        # a run is refused while some headroom remains — and that is the right
        # trade for a spend limit, where overshooting costs real money and
        # under-using costs one deferred analysis.
        def _admit(cap: float | None, window_ms: int, reason: str, label: str) -> None:
            nonlocal cumulative_cap_reason
            if cumulative_cap_reason is not None or cap is None or cap <= 0:
                return
            spent = s.spend_since(now_for_caps - window_ms)
            per_run = cfg.spend_cap_usd or 0.0
            if spent + per_run > cap:
                cumulative_cap_reason = reason
                log.info(
                    f"agent layer skipped: {label} cap would be breached",
                    run_id=run_id,
                    spent_usd=round(spent, 4),
                    per_run_cap_usd=per_run,
                    projected_usd=round(spent + per_run, 4),
                    cap_usd=cap,
                )

        _admit(
            cfg.spend_cap_daily_usd, 24 * 3600 * 1000,
            "agent_skipped_cumulative_cap_daily", "daily",
        )
        _admit(
            cfg.spend_cap_monthly_usd, 30 * 24 * 3600 * 1000,
            "agent_skipped_cumulative_cap_monthly", "monthly",
        )

        # The SDK's own pre-call gate reads `cfg.spend_cap_usd`, so lowering a
        # local variable would have been theatre: `call_claude` would still
        # have authorised the FULL configured per-run cap and spent straight
        # through the cumulative ceiling. The reduced budget therefore has to
        # travel in the config the agents actually receive.

        # 3. Per-project: deterministic analyze + agent layer + compiler
        from .agents import AgentContext
        from .agents import compiler as compiler_agent
        from .agents import friends as friends_agent
        from .agents import research as research_agent
        from .agents import teacher as teacher_agent
        analysis_units: list[_ProjectAnalysisUnit] = []
        for project_path in sorted(corr.project_paths):
            if not Path(project_path).is_dir():
                continue
            project_sessions = tuple(
                bundle for bundle in corr.sessions if bundle.project_path == project_path
            )
            project_turns = tuple(
                record
                for record in stats.records
                if record.project_path == project_path and record.record_type == "message"
            )
            since_ms = (
                min(bundle.started_ms for bundle in project_sessions)
                if project_sessions
                else result.started_ms
            )
            analysis_units.append(
                _ProjectAnalysisUnit(
                    project_path=project_path,
                    sessions=project_sessions,
                    turns=project_turns,
                    since_ms=since_ms,
                )
            )
        units_by_project = {unit.project_path: unit for unit in analysis_units}
        analysis_envelopes = map_units(
            analysis_units,
            unit_id=lambda unit: unit.project_path,
            worker=_analyze_project_unit,
            max_workers=cfg.analysis_workers,
        )
        analysis_by_project: dict[str, ResultEnvelope[_ProjectAnalysisResult]] = {
            envelope.unit_id: envelope for envelope in analysis_envelopes
        }

        for envelope in analysis_envelopes:
            log.info(
                "project analysis worker finished",
                project_path=envelope.unit_id,
                started_monotonic=envelope.started_monotonic,
                ended_monotonic=envelope.ended_monotonic,
                duration_seconds=envelope.ended_monotonic - envelope.started_monotonic,
                warnings=len(envelope.warnings),
                errors=len(envelope.errors),
            )

        for project_path in sorted(corr.project_paths):
            # Liveness check (D1 class a). A project root recorded in historical
            # session JSONLs may since have been deleted. Compiling prompts for a
            # vanished tree emits citations that can never resolve — VG-4 counted
            # 3 such roots (a scratch verify dir, a since-archived project, and
            # one of its nested child workspaces).
            #
            # VG-S1 already classifies the same roots as `path-missing` under
            # excluded_non_repo_paths. Two subsystems observed the dead root;
            # only one acted on it. This makes the behaviour consistent.
            #
            # Emit a finding rather than skipping silently: a project that
            # disappeared is exactly what this tool exists to notice. Low
            # severity because archiving a project is normal and expected.
            if not Path(project_path).is_dir():
                msg = f"project path missing, skipping: {project_path}"
                result.warnings.append(msg)
                result.warning_count += 1
                log.warn("project path missing, skipping", project_path=project_path)
                missing_project_id = persisted.project_ids.get(project_path)
                if missing_project_id is not None:
                    try:
                        # Category matters. `docs_drift` would render through
                        # _finding_line AND _evidence_citations into artifact
                        # bodies (evidence_projection.py:271,286) — and
                        # coldstart.py:104 resolves every absolute path it finds
                        # there. Re-emitting the dead root as prose would
                        # recreate the very broken citation this fix removes.
                        # `incomplete_work` renders the summary line only.
                        _persist_findings(s, missing_project_id, [{
                            "category": "incomplete_work",
                            "severity": "low",
                            "title": "Project directory no longer exists",
                            "detail": (
                                "Recorded in session history but absent from disk at this run. "
                                "Analysis and prompt generation were skipped, so no citation can "
                                "point into a tree that is gone. Expected if the project was "
                                "archived, moved, or deleted."
                            ),
                            # The dead root is kept for operators. Note the
                            # protection is the CATEGORY, not the quoting:
                            # coldstart's path regex ([A-Za-z0-9_./-]+) matches
                            # happily inside quotes, verified. What keeps this
                            # out of artifact bodies is that `incomplete_work`
                            # renders the summary line only, never
                            # _evidence_citations. No paths *inside* the tree are
                            # recorded — each would be unresolvable in turn.
                            "evidence": {
                                "missing_project": f"{project_path!r}",
                                "liveness": "path-missing",
                            },
                            "confidence": "high",
                        }], run_id=run_id)
                    except Exception as exc:  # noqa: BLE001 — never-raise contract
                        log.warn("could not persist missing-project finding",
                                 project_path=project_path, error=str(exc))
                continue

            try:
                # Reuse the id persisted with real session spans; re-upserting
                # here with synthetic now-30d/now would clobber first/last_seen_ms.
                project_id = persisted.project_ids.get(project_path)
                if project_id is None:
                    log.warn("project not persisted, skipping", project_path=project_path)
                    continue
                envelope = analysis_by_project[project_path]
                for warning in envelope.warnings:
                    result.warnings.append(warning)
                    result.warning_count += 1
                    log.warn("project analysis warning", project_path=project_path, warning=warning)
                if envelope.errors or envelope.result is None:
                    result.error_count += 1
                    log.error(
                        "project analysis failed",
                        project_path=project_path,
                        errors=envelope.errors,
                    )
                    continue
                # Named distinctly from the `project_sessions` tuple built in
                # the unit-construction loop above. Reusing that name here
                # rebinds it to a list mid-function, which reads as though the
                # two loops share state when they do not — and leaves a stale
                # binding visible to anything added between the loops.
                unit_sessions = list(units_by_project[project_path].sessions)
                analysis = envelope.result.analysis
                git_evidence = envelope.result.git_evidence
                # R13: mark this project as actually analysed BEFORE persisting
                # findings. A finding absent from this run can then be read as
                # "checked, no longer reported" rather than "never looked at".
                s.record_analyzed_project(run_id, project_id, int(time.time() * 1000))
                # R14: persist the git state THIS run observed. The bundle
                # reads it back through ReadStore instead of shelling out at
                # export time, so an archived bundle can never pair a report
                # from one moment with a working tree from another.
                # `git_capture`, not `git_evidence`: a FAILED read must land
                # too, so the bundle can say "git refused at capture time"
                # rather than falling back to "no snapshot recorded", which
                # is a different fact.
                git_capture = envelope.result.git_capture
                if git_capture is not None:
                    try:
                        s.upsert_git_snapshot(
                            run_id, project_id, git_capture, int(time.time() * 1000)
                        )
                    except Exception as exc:  # noqa: BLE001
                        result.warning_count += 1
                        log.warn(
                            "git snapshot not persisted",
                            project_id=project_id,
                            error=str(exc),
                        )
                for ep in analysis.episodes:
                    # Pass the JSON-encoded classifier evidence so _persist_episodes
                    # stores it under completion_evidence_json (see _persist_episodes).
                    _persist_episodes(s, project_id, [{
                        "started_ms": ep.started_ms,
                        "ended_ms": ep.ended_ms,
                        "original_intent": ep.original_intent,
                        "completion_status": ep.completion_status,
                        "completion_evidence": ep.completion_evidence,
                        "drift_type": ep.drift_type,
                        "pivot_point_ms": ep.pivot_point_ms,
                    }])
                _persist_findings(s, project_id, [
                    {
                        "category": f.category,
                        "severity": f.severity,
                        "title": f.title,
                        "detail": f.detail,
                        "evidence": f.evidence,
                        "confidence": f.confidence,
                        # R10/T3: provenance must be non-null on every finding so a
                        # reader can always tell mechanical fact from model inference.
                        "provenance": getattr(f, "provenance", "deterministic") or "deterministic",
                    }
                    for f in analysis.findings
                ], run_id=run_id)
                result.finding_count += len(analysis.findings)

                # Paid agents and deterministic compiler are gated by
                # cfg.agents_enabled. The paid trio (research/teacher/friends)
                # is what the spend cap governs. The compiler is deterministic;
                # it does NOT count toward the cap and is never skipped due
                # to budget exhaustion — only the user's `agents_enabled`
                # config can disable it.
                from . import agent_sdk  # local import keeps module-load cold
                paid_on = any(
                    cfg.agents_enabled.get(k, False)
                    for k in ("research", "teacher", "friends")
                )
                cap_positive = (
                    cfg.spend_cap_usd is not None and cfg.spend_cap_usd > 0
                )
                project_status_note = None
                if paid_on and cumulative_cap_reason is not None:
                    # I3: cumulative daily/monthly cap already reached
                    # (computed once, pre-flight, above the per-project
                    # loop). Deliberately does NOT transition
                    # `agent_run_status` to `partial` the way the
                    # "no per-run cap configured" branch below does: the
                    # assignment reuses the existing `disabled` status
                    # verbatim so downstream surfaces (report, `dreamy
                    # status`) need no new state to recognise this case —
                    # a cumulative-cap skip reads exactly like "paid
                    # agents are off", which for this run's purposes they
                    # effectively are.
                    project_status_note = cumulative_cap_reason
                    result.skipped_projects.append({
                        "project_id": project_id,
                        "project_path": project_path,
                        "reason": project_status_note,
                    })
                    log.info(
                        "agent skipped: cumulative cap reached",
                        project_id=project_id,
                        project_path=project_path,
                        reason=project_status_note,
                    )
                elif paid_on and not cap_positive:
                    # Null / non-positive cap -> paid analysis disabled.
                    # Compiler still runs (it is deterministic). Emit a
                    # single skip record per project so the report shows
                    # the disabled reason.
                    project_status_note = "agent_disabled_no_cap"
                    result.skipped_projects.append({
                        "project_id": project_id,
                        "project_path": project_path,
                        "reason": project_status_note,
                    })
                    log.info(
                        "agent skipped: no cap configured",
                        project_id=project_id,
                        project_path=project_path,
                    )
                    if result.agent_run_status == "disabled":
                        result.agent_run_status = "partial"
                elif paid_on and cap_positive:
                    spent = agent_sdk._sum_run_cost(s, run_id)
                    remaining = cfg.spend_cap_usd - spent
                    max_call_usd = agent_sdk._max_call_cost_usd(s, run_id)
                    # Cost-aware skip (mirrors call_claude's pre-call
                    # gate): once we've seen a paid call, skip a project
                    # when `remaining < max_observed_per_call_cost`.
                    # Without the max_call_usd row we cannot prove a
                    # next call would overshoot (and the user's
                    # "1.5×probe" cap arithmetic depends on it).
                    if max_call_usd is not None and remaining < max_call_usd:
                        project_status_note = "agent_skipped_cap_insufficient_remaining"
                        result.skipped_projects.append({
                            "project_id": project_id,
                            "project_path": project_path,
                            "reason": project_status_note,
                            "remaining_usd": round(remaining, 4),
                            "max_call_usd": round(max_call_usd, 4),
                            "cap_usd": cfg.spend_cap_usd,
                        })
                        log.info(
                            "agent skipped: remaining < max-observed call",
                            project_id=project_id,
                            project_path=project_path,
                            remaining_usd=round(remaining, 4),
                            max_call_usd=round(max_call_usd, 4),
                            cap_usd=cfg.spend_cap_usd,
                        )
                        if spent > 0:
                            result.agent_run_status = "capped"
                    elif remaining <= 0:
                        project_status_note = "agent_skipped_cap_reached"
                        result.skipped_projects.append({
                            "project_id": project_id,
                            "project_path": project_path,
                            "reason": project_status_note,
                        })
                        log.info(
                            "agent skipped: cap already exhausted",
                            project_id=project_id,
                            project_path=project_path,
                        )
                        if spent > 0:
                            result.agent_run_status = "capped"
                rctx = AgentContext(
                    project_id=project_id,
                    project_path=project_path,
                    project_path_slug=Path(project_path).name,
                    cfg=cfg,
                    output_dir=output_dir,
                    run_id=run_id,
                    store=s,
                    log=logging_util.get_logger("research"),
                    evidence={
                        "findings": [f.__dict__ for f in analysis.findings],
                        "episodes": [ep.__dict__ for ep in analysis.episodes],
                        "original_intent": analysis.episodes[0].original_intent if analysis.episodes else "",
                        "anchor_ms": result.started_ms,
                        "now_ms": int(time.time() * 1000),
                        "git": git_evidence.__dict__ if git_evidence is not None else {},
                        "sessions": [b.__dict__ for b in unit_sessions],
                        "next_task_seeds": list(analysis.next_task_seeds),
                        "project": {
                            "id": project_id,
                            "path": project_path,
                            "episodes": [ep.__dict__ for ep in analysis.episodes],
                        },
                    },
                    skip_reason=project_status_note,
                )
                # If the project was skipped on the cap, the paid trio
                # exits fast — compiler still runs.
                # Per-agent ledger re-check: BEFORE every paid agent,
                # re-read `_sum_run_cost` so a previous agent's actual
                 # cost (already flushed to the ledger) is honored.
                # If remaining ≤ 0, mark THIS project (and any later)
                 # as `agent_skipped_late` and exit the trio without
                 # raising. Compiler still runs (deterministic).
                # Inner per-agent gate. Mirrors call_claude's
                # cost-aware skip: once a paid call has completed,
                # we know the size of one real call (max_observed_call
                # via `agent_calls` ledger). If `remaining < that`,
                # no second paid call can fit. We track `paid_done`
                # LOCALLY (without mutating `cfg.agents_enabled`)
                # so the loop state is informational; later projects
                # in the same run still need truthful skip records,
                # not a permanently-disabled flag.
                if project_status_note is None:
                    paid_done: dict[str, int] = {}
                    for _key, _fn in (
                        ("research", research_agent.run),
                        ("teacher", teacher_agent.run),
                        ("friends", friends_agent.run),
                    ):
                        if not cfg.agents_enabled.get(_key, False):
                            continue
                        if paid_done.get(_key, 0) > 0:
                            continue
                        spent_now = agent_sdk._sum_run_cost(s, run_id)
                        remaining = cfg.spend_cap_usd - spent_now
                        max_call_usd = agent_sdk._max_call_cost_usd(s, run_id)
                        skip_reason = None
                        if max_call_usd is not None and remaining < max_call_usd:
                            skip_reason = "agent_skipped_late_insufficient_remaining"
                        elif remaining <= 0:
                            skip_reason = "agent_skipped_late_cap_reached"
                        if skip_reason is not None:
                            result.skipped_projects.append({
                                "project_id": project_id,
                                "project_path": project_path,
                                "reason": skip_reason,
                                "remaining_usd": round(remaining, 4),
                                "max_call_usd": round(max_call_usd, 4) if max_call_usd else None,
                            })
                            if spent_now > 0:
                                result.agent_run_status = "capped"
                            rctx.log.info(
                                "agent skipped mid-run",
                                agent_type=_key,
                                project_id=project_id,
                                reason=skip_reason,
                                remaining_usd=round(remaining, 4),
                                max_call_usd=round(max_call_usd, 4) if max_call_usd else None,
                            )
                            continue
                        try:
                            _fn(rctx)
                            result.prompt_count += 1
                            paid_done[_key] = paid_done.get(_key, 0) + 1
                        except agent_sdk.SpendCapExceeded as exc:
                            _mark_agent_skipped_legacy(
                                str(exc), project_id, project_path, result,
                            )
                            # No mutation of cfg.agents_enabled.
                            continue
                # Compiler is deterministic; it ALWAYS runs when enabled,
                # regardless of paid-agent skip status.
                if cfg.agents_enabled.get("compiler", True):
                    compiler_agent.run(rctx)
                    result.prompt_count += 1

            except Exception as exc:  # noqa: BLE001
                result.error_count += 1
                # Keep the traceback: str(exc) alone leaves a failure
                # forensically dead (see VG-4 coldstart-artifact-finding.md,
                # where 17 identical project failures became unreproducible
                # because only the message survived).
                log.error(
                    "project failed",
                    project_path=project_path,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    traceback=traceback.format_exc(),
                )

        # Aggregate agent-call ledger rows for the report + gate evidence.
        rows = s.conn.execute(
            "SELECT id, agent_type, model, started_ms, ended_ms, status, cost_usd, "
            "prompt_tokens, completion_tokens, input_fingerprint, output_json, error_text "
            "FROM agent_calls WHERE run_id=? ORDER BY started_ms",
            (run_id,),
        ).fetchall()
        result.agent_call_records = [dict(r) for r in rows]
        # Total cost authoritative — sum of ledger, never the per-call field.
        total_paid = sum(float(r["cost_usd"] or 0.0) for r in result.agent_call_records)
        result.agent_cost_usd = total_paid
        # Count persisted `agent` findings (provenance=agent). The agents
        # write via Store.insert_finding_with_provenance(... provenance=
        # "agent"); on rerun, content-hash-stable_id is idempotent so
        # idempotent — the count is the truth-of-the-DB at end of run.
        row = s.conn.execute(
            "SELECT COUNT(*) FROM findings WHERE provenance='agent'"
        ).fetchone()
        result.agent_findings_persisted = int(row[0]) if row else 0
        # If at least one paid call completed AND the loop also skipped
        # projects, the run is `capped`. With calls but no skips, it is
        # `completed`. With no calls and no paid-agents configured, it
        # stays `disabled`.
        if total_paid > 0 and len(result.agent_call_records) > 0:
            if result.skipped_projects:
                if result.agent_run_status not in ("capped",):
                    result.agent_run_status = "capped"
            elif result.agent_run_status not in ("completed",):
                result.agent_run_status = "completed"
        # I3: one spend_ledger row per run, written HERE — after the agent
        # layer has fully settled (`total_paid`/`agent_call_records` above
        # are already the ledger-authoritative totals for this run) and
        # BEFORE the commit two lines below, so the ledger row and the
        # `agent_calls` rows it summarises land in the same transaction.
        # A later run's pre-flight cumulative check (top of this function)
        # reads this table, so a run that never reaches this line (crash,
        # `status="lock_held"` early return) correctly contributes nothing
        # to a future cumulative total — it never spent anything either.
        last_model = result.agent_call_records[-1]["model"] if result.agent_call_records else None
        s.record_run_spend(
            run_id, result.started_ms, total_paid, last_model,
            len(result.agent_call_records),
        )
        # Commit pending writes (agent_calls inserts + findings upserts).
        # Fatal, not warn-only: `_reconcile_compiler_stamps` runs
        # immediately after and reads `prompt_artifacts` content via the
        # SAME connection to decide fresh-vs-stale. A same-connection SELECT
        # sees this transaction's writes regardless of whether THIS commit
        # succeeds — but if it silently fails, compiler-loop writes could be
        # lost on crash/reopen while phase B still reports success against
        # the in-memory (uncommitted) view. D24's whole point is a verdict
        # that can be trusted; a swallowed commit failure here would let
        # phase B claim "fresh" on data that never reached disk.
        s.commit()

        # D24, phase B: settle rows the compile loop just touched (fresh
        # `compiler=<stamp>` -> un-archive) and archive+unlink anything
        # STILL mismatched — inactive projects and phantom paths the loop
        # above never visits. See `_reconcile_compiler_stamps` docstring.
        _reconcile_compiler_stamps(s, output_dir, cfg, log, run_id)

        # Artifacts are written by the compiler agent, which rewrites evidence
        # paths for the artifact BODY only. Re-reading them here and rewriting
        # the whole file again also mangled the inlined skill documentation —
        # turning illustrative placeholders like `evidence/vg{N}-{desc}.{ext}`
        # into absolute paths that can never exist, which then fail cold start.
        # Reconcile disk against the DB instead of re-transforming content.
        #
        # `archived_ms IS NULL` (D24): only rows `_reconcile_compiler_stamps`
        # just confirmed carry a stamp matching TODAY's compiler are written
        # here. An archived row's file was already unlinked (or replaced
        # with the stale placeholder) by the reconcile pass above — this
        # loop must never re-write it, or a stale row's bytes would be
        # served again on every subsequent run purely because they were
        # still sitting in the `content` column.
        rows = s.conn.execute(
            "SELECT pa.prompt_type, pa.content, p.path "
            "FROM prompt_artifacts pa JOIN projects p ON p.id=pa.project_id "
            "WHERE pa.archived_ms IS NULL "
            "ORDER BY pa.project_id, pa.prompt_type"
        ).fetchall()
        for row in rows:
            import hashlib

            slug = hashlib.sha256(row["path"].encode("utf-8")).hexdigest()[:12]
            prompt_dir = output_dir / "reports" / "latest" / "projects" / slug / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            # Ensure the evidence dir referenced by the artifact body
            # (under output_dir) exists, so the reader can actually run the
            # capture step the artifact instructs. The artifacts baked
            # `<out>/reports/latest/projects/<slug>/evidence` as the
            # capture target; if the dir is absent on the reader's
            # filesystem, coldstart flags it as a broken citation.
            evidence_dir = output_dir / "reports" / "latest" / "projects" / slug / "evidence"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            evidence_dir.touch()
            target = prompt_dir / f"{row['prompt_type']}.md"
            # Reconcile means the DB wins, always. A write-if-absent left any
            # artifact emitted by an older compiler in place forever: after the
            # D17 skill-name repair, 17 files under reports/latest/ still named
            # `akresearch-prompt` across a full successful run, because the file
            # already existed. The row here IS the current compiler's output, so
            # disk must mirror it rather than defer to whatever landed first.
            # Write only on change to keep mtimes meaningful as a freshness
            # signal, and atomically so a crash cannot truncate a live artifact.
            content = row["content"]
            try:
                current = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                current = None
            if current != content:
                temp = target.with_suffix(target.suffix + ".tmp")
                temp.write_text(content, encoding="utf-8")
                os.replace(temp, target)

        result.status = "ok"
        # 4. Reports — skipped if TUI/agent module unavailable; always best-effort.
        try:
            from . import reports as reports_mod

            read_store = ReadStore(db_path=db_path, output_dir=output_dir)
            reports_mod.write_executive_report(output_dir, run_id, cfg, result, read_store)
            log.info("executive report written", run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            result.warning_count += 1
            log.warn("report failed", run_id=run_id, error=str(exc))

        # 5. Retention purge (best-effort, never fatal)
        try:
            from . import retention

            if hasattr(cfg, "retention_days") and cfg.retention_days > 0:
                retention.purge_old_artifacts(output_dir, cfg.retention_days)
        except Exception as exc:  # noqa: BLE001
            log.warn("retention failed", run_id=run_id, error=str(exc))

    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.error_count += 1
        log.error("pipeline failed", run_id=run_id, error=str(exc))
    finally:
        result.ended_ms = int(time.time() * 1000)
        try:
            s.finish_run(
                run_id,
                result.ended_ms,
                result.status,
                source_counts_json=__import__("json").dumps(result.source_counts),
                warning_count=result.warning_count,
                error_count=result.error_count,
                log_path=str(output_dir / "logs"),
            )
        except Exception:
            pass
        try:
            s.commit()
        except Exception:
            pass
        try:
            if lock is not None:
                lock.release()
        except Exception:
            pass
        # Close the SQLite connection: leaking it holds the WAL write lock and
        # makes any subsequent run in this process fail with "database is locked".
        try:
            s.close()
        except Exception:
            pass

    log.info(
        "pipeline done", run_id=run_id, status=result.status,
        finding_count=result.finding_count, prompt_count=result.prompt_count,
    )
    return result
