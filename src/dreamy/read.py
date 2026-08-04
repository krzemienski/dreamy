"""Read layer — TUI/web-shared data access. Plain dataclasses only; no SQL in widget code.

Per R17, future web dashboards must reuse this layer. Per R8, TUI pilot tests
must show real data from this layer, not just "launches without error".
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

# ---------------------------------------------------------------------------
# Dataclasses — shape contracts. Widgets depend ONLY on these.
# ---------------------------------------------------------------------------

@dataclass
class RunSummary:
    id: str
    started_ms: int
    ended_ms: int | None
    status: str
    duration_ms: int | None
    finding_count: int = 0
    prompt_count: int = 0
    agent_cost_usd: float = 0.0
    source_counts: dict = field(default_factory=dict)
    lookback_days: int | None = None


def _duration_ms(row) -> int | None:
    """Wall-clock duration of a run, or None when it has no meaningful one.

    `interrupted` runs are excluded deliberately. Their `ended_ms` is the
    RECOVERY timestamp — the moment a later run noticed the orphan — not the
    moment the process died, which nothing recorded. Measured example: a run
    killed ~1s in reports `dur=16.1s` because recovery happened 16s later.
    Reporting that as a duration would silently inflate the N1 incremental-run
    budget with time no run actually spent working.
    """
    if row["ended_ms"] is None:
        return None
    if (row["status"] or "") == "interrupted":
        return None
    return row["ended_ms"] - row["started_ms"]


@dataclass
class SourceStat:
    source_id: str
    available: bool
    record_count: int = 0
    path: str = ""
    error: str | None = None
    # Distinct from `available`, which is per-window activity. True once a
    # source has ever been successfully ingested (it has a watermark row), so
    # a quiet window cannot be misread as an uninstalled harness.
    ever_ingested: bool = False
    # I9 — derived cadence label. None when no baseline has been computed
    # yet; otherwise one of "ok", "stale", "insufficient data". Existing
    # field meanings are unchanged: web dashboard and TUI read available /
    # ever_ingested / record_count as before.
    cadence: str | None = None


@dataclass
class ProjectSummary:
    id: str
    path: str
    name: str
    first_seen_ms: int
    last_seen_ms: int
    git_remote: str | None = None
    session_count: int = 0


@dataclass
class FindingRow:
    id: str
    project_id: str
    category: str
    severity: str
    title: str
    detail: str
    confidence: str
    created_ms: int
    resolved_ms: int | None
    dismissal_reason: str | None
    delta_state: str = "new"  # new | persisting | resolved | regressed
    provenance: str = "D"  # D = deterministic, A = agent


@dataclass
class FindingDiff:
    finding_id: str
    state: str  # new | persisting | resolved | regressed
    category: str
    severity: str


@dataclass
class FindingMetric:
    """R22 — aggregated finding-lifecycle stats for one category or severity
    bucket. Every field is DERIVED from `finding_observations` on read, same
    reasoning as `_delta_states`: a stored lifecycle-metrics row would need
    recomputing on every run anyway (each new run can move any finding into a
    new bucket), so a stored value could only ever be stale or redundant.

    `not_observed` is a first-class count (R21 vocabulary), never merged into
    a denominator. A finding whose owning project has never been analysed
    tells us nothing about resolution speed or regression risk — counting it
    either way would manufacture a rate out of missing data.
    """
    key: str  # category name or severity name
    not_observed: int = 0
    resolve_events: int = 0
    time_to_resolve_ms: float | None = None
    regression_rate: float | None = None
    backlog_delta: int = 0
    open_count: int = 0
    age_p50_ms: float | None = None
    age_p90_ms: float | None = None


@dataclass
class EpisodeRow:
    id: str
    project_id: str
    started_ms: int
    ended_ms: int | None
    original_intent: str
    completion_status: str
    drift_type: str | None
    pivot_point_ms: int | None


@dataclass
class ProjectDetail:
    summary: ProjectSummary
    episodes: list[EpisodeRow]
    prompt_artifacts: list[PromptRow]
    cost_30d: CostByProject | None


@dataclass
class PromptRow:
    id: str
    project_id: str
    prompt_type: str
    stable_hash: str
    created_ms: int


@dataclass
class CostByProject:
    project_id: str
    total_usd: float
    episode_count: int
    per_completed_intent_usd: float
    confidence_mix: dict


@dataclass
class TopicEvent:
    ts_ms: int
    topic: str
    level: str
    msg: str
    project_id: str | None = None
    agent_type: str | None = None


@dataclass
class GitSnapshot:
    """Git state as of a specific run — never re-read from the live repo."""
    run_id: str
    project_id: str
    captured_ms: int
    status_porcelain: str
    recent_log: list[dict]
    diff_stat: str
    error: str | None = None

@dataclass
class ScheduleState:
    label: str
    installed: bool
    interval_seconds: int
    next_run_ms: int | None
    plist_path: str
    last_exit: int | None
    raw: str


@dataclass
class PromptHealth:
    artifact_path: str
    citations_total: int
    citations_broken: int
    sections_present: int
    sections_required: int
    cold_start_pass: bool
    evidence_dir_abs: bool
    skill_refs: int
    provenance_marker: bool


# ---------------------------------------------------------------------------
# ReadStore — wrappers around the Store. No raw SQL in widget code.
# ---------------------------------------------------------------------------

class ReadStore:
    def __init__(self, db_path: Path, output_dir: Path | None = None, *, read_only: bool = False):
        from . import store as _store_mod
        # `read_only` is opt-in rather than the default because the TUI shares
        # this class and still needs the R20 dismiss/undismiss write path. The
        # web surface (R17d) passes it so a browser request physically cannot
        # write, migrate, or create the state file.
        self._store = _store_mod.Store(db_path, read_only=read_only)
        self._read_only = read_only
        self._output_dir = output_dir
        self._db_path = db_path

    def close(self):
        self._store.close()

    # ------- runs -------
    def latest_run(self) -> RunSummary | None:
        row = self._store.conn.execute(
            "SELECT id, started_ms, ended_ms, status, source_counts_json, lookback_days "
            "FROM runs ORDER BY started_ms DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        sc = {}
        if row["source_counts_json"]:
            try:
                sc = json.loads(row["source_counts_json"])
            except json.JSONDecodeError:
                sc = {}
        return RunSummary(
            id=row["id"],
            started_ms=row["started_ms"],
            ended_ms=row["ended_ms"],
            status=row["status"] or "unknown",
            duration_ms=_duration_ms(row),
            source_counts=sc,
            lookback_days=row["lookback_days"],
        )

    def runs_history(self, limit: int = 20) -> list[RunSummary]:
        rows = self._store.conn.execute(
            "SELECT id, started_ms, ended_ms, status, source_counts_json, lookback_days "
            "FROM runs ORDER BY started_ms DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for row in rows:
            sc = {}
            if row["source_counts_json"]:
                try:
                    sc = json.loads(row["source_counts_json"])
                except json.JSONDecodeError:
                    sc = {}
            out.append(
                RunSummary(
                    id=row["id"],
                    started_ms=row["started_ms"],
                    ended_ms=row["ended_ms"],
                    status=row["status"] or "unknown",
                    duration_ms=_duration_ms(row),
                    source_counts=sc,
                    lookback_days=row["lookback_days"],
                )
            )
        return out

    def total_agent_spend(self) -> float:
        row = self._store.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM agent_calls"
        ).fetchone()
        return float(row["total"] or 0.0)

    def run(self, run_id: str) -> RunSummary | None:
        """One run by id — NOT "the newest run".

        A report describes a specific run, so it must read that run's own
        window. `latest_run()` is ordered by `started_ms`, which ties when two
        runs start in the same millisecond and can also return a run started
        concurrently by another process — either way the report would state a
        window it did not use.
        """
        row = self._store.conn.execute(
            "SELECT id, started_ms, ended_ms, status, source_counts_json, lookback_days "
            "FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        sc = {}
        if row["source_counts_json"]:
            try:
                sc = json.loads(row["source_counts_json"])
            except json.JSONDecodeError:
                sc = {}
        return RunSummary(
            id=row["id"],
            started_ms=row["started_ms"],
            ended_ms=row["ended_ms"],
            status=row["status"] or "unknown",
            duration_ms=_duration_ms(row),
            source_counts=sc,
            lookback_days=row["lookback_days"],
        )

    def git_snapshot(self, project_id: str, run_id: str | None = None) -> GitSnapshot | None:
        """Persisted git state for a project (R14) — never newer than `run_id`.

        With `run_id`, returns that run's own snapshot if it captured one;
        otherwise the most recent snapshot captured AT OR BEFORE that run.
        A run only snapshots projects it analysed, so a quiet run legitimately
        captures nothing — falling back keeps the bundle useful, while the
        "at or before" bound preserves the guarantee that an archived bundle
        never shows a working tree NEWER than the report beside it. The caller
        renders `captured_ms` and the capturing run id, so an older snapshot
        is visibly older rather than silently passed off as current.
        """
        if run_id:
            # Exact match first: the run's own snapshot is always preferred.
            row = self._store.conn.execute(
                "SELECT * FROM project_git_snapshots WHERE project_id=? AND run_id=?",
                (project_id, run_id),
            ).fetchone()
            if row is None:
                # Fall back to the newest snapshot STRICTLY BEFORE this run.
                # No COALESCE: an unknown `run_id` yields NULL, the predicate
                # is never true, and the query returns nothing — asking about
                # a run that does not exist must not silently hand back the
                # newest snapshot, which is the inverse of the guarantee.
                # Strict `<` (not `<=`) on started_ms so a sibling run that
                # began in the same millisecond can never win the tie.
                row = self._store.conn.execute(
                    "SELECT s.* FROM project_git_snapshots s "
                    "JOIN runs r ON r.id = s.run_id "
                    "WHERE s.project_id=? "
                    "  AND r.started_ms < (SELECT started_ms FROM runs WHERE id=?) "
                    "ORDER BY r.started_ms DESC, s.captured_ms DESC, s.run_id DESC LIMIT 1",
                    (project_id, run_id),
                ).fetchone()
        else:
            row = self._store.conn.execute(
                "SELECT * FROM project_git_snapshots WHERE project_id=? "
                "ORDER BY captured_ms DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            log = json.loads(row["recent_log_json"] or "[]")
        except json.JSONDecodeError:
            log = []
        return GitSnapshot(
            run_id=row["run_id"],
            project_id=row["project_id"],
            captured_ms=row["captured_ms"],
            status_porcelain=row["status_porcelain"] or "",
            recent_log=log,
            diff_stat=row["diff_stat"] or "",
            error=row["error"],
        )

    # ------- archival import (I5) -------
    def imported_bundles(self) -> list[dict]:
        """Imported archival snapshots, newest first. Read-only.

        Deliberately does NOT join to `projects`, `sessions`, or `turns`: an
        imported bundle shares no key space with locally-ingested data, and a
        join that appeared to work would imply an equivalence that does not
        exist.
        """
        rows = self._store.conn.execute(
            "SELECT bundle_id, project_name, source_file, exported_ms, imported_ms, "
            "session_count, turn_count FROM imported_bundles ORDER BY imported_ms DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------- portable export (I5) -------
    def export_sessions(self, project_id: str, mac_key: bytes) -> list[dict]:
        """I5 — a project's session/turn structure, for portable archival export.

        Contract: **opaque per-bundle ids, timestamps, and counts.** No prose,
        no filesystem paths, no URLs, and no canonical identifier that could
        be tested against a guess.

        Columns dropped, each established by auditing the real store rather
        than by reading a column name:

        * `turns.content_fingerprint` is NOT a hash despite its name. Measured
          across the FULL live population (444,415 rows, not a sample): length
          0–207, median 73 counting the 34% empty rows and 181 among populated
          ones — TRUNCATED USER PROSE, with 5,895 rows containing a URL and
          40,522 an absolute path.
        * `sessions.native_id` is an absolute `$HOME` path for claude and omp
          (6,071 of 11,129 sessions store-wide).
        * `sessions.raw_path` carries `$HOME` in 11,129 of 11,129 rows.
        * `turns.error_text` is free-form error prose. 0 populated rows today,
          excluded by RULE so the next harness to fill it cannot leak silently.
        * `turns.file_paths_json` — 444,415 rows, 0 with `$HOME`, dropped
          anyway: repo-relative paths disclose directory structure and
          unreleased product names.
        * `sessions.model` / `turns.role` — dropped. They are neither identity
          nor timestamp, and the contract is not "mostly identity".

        Canonical `sessions.id` / `turns.id` are ALSO withheld. They look
        opaque — 16 hex chars, no `$HOME` — but `stable_id` is an unsalted
        hash of guessable inputs, and that was demonstrated, not assumed:
        given only an exported id, recomputing
        `stable_id("session", "claude", "~/.claude/projects/-/<uuid>")`
        reproduced it exactly, while three wrong guesses did not. Shipping
        canonical ids would let any bundle holder confirm the sender's account
        name and file layout offline.

        Instead each row gets a per-bundle export id: an HMAC of the canonical
        id under `mac_key`, a random key generated per bundle and DISCARDED by
        the caller after export. Because the key never ships:

        * a guess cannot be confirmed — there is nothing to hash against;
        * two bundles of the same project share no ids, so they cannot be
          correlated;
        * re-importing the SAME FILE stays idempotent, since the ids live in
          the payload.

        The cost is stated plainly: a receiver cannot merge two exports of one
        project, because nothing links them. Each bundle is a self-contained
        archival snapshot, which is exactly what I5's two use cases need —
        moving history to another machine, and archiving before deleting a
        checkout. Neither requires cross-bundle merge.

        Parent/child structure survives because turns are nested inside their
        session rather than carrying a foreign key.
        """
        import hmac
        from hashlib import sha256

        if not mac_key:
            raise ValueError("export_sessions requires a per-bundle mac_key")

        def _eid(canonical: str) -> str:
            return hmac.new(mac_key, canonical.encode("utf-8"), sha256).hexdigest()[:32]

        srows = self._store.conn.execute(
            "SELECT id, started_ms, ended_ms, record_count "
            "FROM sessions WHERE project_id=? ORDER BY started_ms",
            (project_id,),
        ).fetchall()
        out: list[dict] = []
        for s in srows:
            trows = self._store.conn.execute(
                "SELECT id, timestamp_ms FROM turns WHERE session_id=? ORDER BY timestamp_ms",
                (s["id"],),
            ).fetchall()
            out.append({
                "export_id": _eid(s["id"]),
                "started_ms": s["started_ms"],
                "ended_ms": s["ended_ms"],
                "record_count": s["record_count"] or 0,
                "turns": [
                    {"export_id": _eid(t["id"]), "timestamp_ms": t["timestamp_ms"]}
                    for t in trows
                ],
            })
        return out

    # ------- projects -------
    def all_projects(self) -> list[ProjectSummary]:
        rows = self._store.conn.execute(
            "SELECT p.id, p.path, p.name, p.first_seen_ms, p.last_seen_ms, p.git_remote, "
            "(SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id) AS sc "
            "FROM projects p ORDER BY p.last_seen_ms DESC"
        ).fetchall()
        return [
            ProjectSummary(
                id=r["id"],
                path=r["path"],
                name=r["name"] or r["path"],
                first_seen_ms=r["first_seen_ms"],
                last_seen_ms=r["last_seen_ms"],
                git_remote=r["git_remote"],
                session_count=r["sc"],
            )
            for r in rows
        ]

    def project_detail(self, project_id: str) -> ProjectDetail | None:
        prow = self._store.conn.execute(
            "SELECT id, path, name, first_seen_ms, last_seen_ms, git_remote FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
        if prow is None:
            return None
        sc = self._store.conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE project_id=?", (project_id,)
        ).fetchone()["c"]
        summary = ProjectSummary(
            id=prow["id"],
            path=prow["path"],
            name=prow["name"] or prow["path"],
            first_seen_ms=prow["first_seen_ms"],
            last_seen_ms=prow["last_seen_ms"],
            git_remote=prow["git_remote"],
            session_count=sc,
        )
        erows = self._store.conn.execute(
            "SELECT id, project_id, started_ms, ended_ms, original_intent, completion_status, "
            "drift_type, pivot_point_ms "
            "FROM intent_episodes WHERE project_id=? ORDER BY started_ms DESC LIMIT 50",
            (project_id,),
        ).fetchall()
        episodes = [
            EpisodeRow(
                id=e["id"],
                project_id=e["project_id"],
                started_ms=e["started_ms"],
                ended_ms=e["ended_ms"],
                original_intent=e["original_intent"] or "",
                completion_status=e["completion_status"] or "unverified",
                drift_type=e["drift_type"],
                pivot_point_ms=e["pivot_point_ms"],
            )
            for e in erows
        ]
        prompts = self.prompt_artifacts(project_id)
        cost = self.cost_30d(project_id)
        return ProjectDetail(summary=summary, episodes=episodes, prompt_artifacts=prompts, cost_30d=cost)

    # ------- findings -------
    def _run_pair(self, run_id: str | None = None) -> tuple[str | None, str | None]:
        """(previous_run_id, current_run_id) for the R13 diff.

        Authority is `run_analyzed_projects`, NOT `finding_observations`. A run
        that analysed a project and found nothing wrong is the most interesting
        run there is — it is what "resolved" means — but it writes no
        observation rows. Keying off observations made such a run invisible, so
        a genuine fix would never show up as `resolved`; the diff would keep
        comparing two older runs and report the finding as still persisting.

        A run that analysed nothing (no sessions in the window, every project
        skipped) is correctly excluded: it looked at nothing, so it can neither
        confirm nor resolve anything.
        """
        rows = self._store.conn.execute(
            "SELECT DISTINCT a.run_id, r.started_ms FROM run_analyzed_projects a "
            "JOIN runs r ON r.id = a.run_id ORDER BY r.started_ms DESC LIMIT 2"
        ).fetchall()
        if not rows:
            return (None, None)
        if run_id:
            prior = self._store.conn.execute(
                "SELECT DISTINCT a.run_id FROM run_analyzed_projects a "
                "JOIN runs r ON r.id = a.run_id "
                "WHERE r.started_ms < (SELECT started_ms FROM runs WHERE id=?) "
                "ORDER BY r.started_ms DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return (prior["run_id"] if prior else None, run_id)
        return (rows[1]["run_id"] if len(rows) > 1 else None, rows[0]["run_id"])

    def _analyzed_projects(self, run_id: str | None) -> set[str]:
        """Projects the given run actually analysed."""
        if run_id is None:
            return set()
        return {
            r["project_id"]
            for r in self._store.conn.execute(
                "SELECT project_id FROM run_analyzed_projects WHERE run_id=?", (run_id,)
            ).fetchall()
        }

    def _observed_before(
        self, finding_id: str, project_id: str, before_ms: int
    ) -> bool:
        """Did any earlier run that analysed this project observe the finding?

        The immediate predecessor analysed the project but has no row for this
        finding, so the analyzer looked and did not report it — that absence
        IS the resolution, and the second loop in `_delta_states` renders it as
        `resolved` without ever writing a row. There is therefore no stored
        `resolved=1` flag to walk back to; prior *existence* is the signal.
        Scoped to runs that actually analysed the project so a run that never
        looked cannot be mistaken for evidence either way.
        """
        row = self._store.conn.execute(
            "SELECT 1 FROM finding_observations o "
            "JOIN runs r ON r.id = o.run_id "
            "JOIN run_analyzed_projects a ON a.run_id = o.run_id AND a.project_id = ? "
            "WHERE o.finding_id = ? AND r.started_ms < ? LIMIT 1",
            (project_id, finding_id, before_ms),
        ).fetchone()
        return row is not None

    def _delta_states(self, run_id: str | None = None) -> dict[str, str]:
        """finding_id -> R13 state, derived from the observation ledger.

        Derived rather than stored. `findings.state` was written as the
        literal 'new' on every upsert, so a stored state could never do
        anything but say `new` — the classification has to be computed
        against the runs that actually observed each finding.

        Comparison is PER PROJECT, not per run. Runs do not analyse a fixed
        project set: a 30-day window covers whichever projects had sessions,
        so run N may cover project A and run N+1 only project B. Comparing
        against one global "previous run" would then diff B's findings against
        A's observations, find no overlap, and report every long-standing
        finding in B as `new`. The correct predecessor is per project: the
        most recent EARLIER run that analysed *that* project.

        A project the current run did not analyse is omitted entirely — it was
        not re-checked, so any verdict about it would be invented.
        """
        _, curr_id = self._run_pair(run_id)
        if curr_id is None:
            return {}
        analyzed = self._analyzed_projects(curr_id)
        if not analyzed:
            return {}

        owners = {
            r["id"]: r["project_id"]
            for r in self._store.conn.execute("SELECT id, project_id FROM findings").fetchall()
        }
        curr_started = self._store.conn.execute(
            "SELECT started_ms FROM runs WHERE id=?", (curr_id,)
        ).fetchone()
        if curr_started is None:
            return {}

        # Per-project predecessor: the latest earlier run that analysed it.
        prev_by_project: dict[str, str] = {}
        for project_id in analyzed:
            row = self._store.conn.execute(
                "SELECT a.run_id FROM run_analyzed_projects a JOIN runs r ON r.id = a.run_id "
                "WHERE a.project_id=? AND r.started_ms < ? ORDER BY r.started_ms DESC LIMIT 1",
                (project_id, curr_started["started_ms"]),
            ).fetchone()
            if row:
                prev_by_project[project_id] = row["run_id"]

        def _obs(rid: str) -> dict[str, bool]:
            return {
                r["finding_id"]: bool(r["resolved"])
                for r in self._store.conn.execute(
                    "SELECT finding_id, resolved FROM finding_observations WHERE run_id=?",
                    (rid,),
                ).fetchall()
            }

        curr_all = _obs(curr_id)
        prev_cache: dict[str, dict[str, bool]] = {}
        states: dict[str, str] = {}

        for fid, resolved_now in curr_all.items():
            # Distinct from the `project_id` loop variable used in the
            # predecessor scan above: `owners.get` may return None for a
            # finding whose project row is gone, and rebinding that name to an
            # optional would both shadow a live binding and change its type.
            owner_project_id = owners.get(fid)
            if owner_project_id is None or owner_project_id not in analyzed:
                continue
            prev_run = prev_by_project.get(owner_project_id)
            if prev_run is None:
                # First time this project was ever analysed: nothing to diff.
                states[fid] = "resolved" if resolved_now else "new"
                continue
            if prev_run not in prev_cache:
                prev_cache[prev_run] = _obs(prev_run)
            prev = prev_cache[prev_run]
            was_seen = fid in prev
            was_resolved = prev.get(fid, False)
            if not was_seen:
                # R13: "resolved in run N, reappearing in run N+2 is
                # `regressed`, not `new`." The predecessor analysed this
                # project but has no row for this finding, so the analyzer
                # looked and did not report it — that absence was rendered as
                # `resolved` by the second loop below and never written as a
                # row. The evidence of the earlier resolution is therefore
                # prior EXISTENCE, not a stored `resolved` flag.
                if self._observed_before(fid, owner_project_id, curr_started["started_ms"]):
                    was_seen = was_resolved = True
            if resolved_now:
                states[fid] = "dismissed" if was_resolved else "resolved"
            elif was_resolved:
                states[fid] = "regressed"
            elif was_seen:
                states[fid] = "persisting"
            else:
                states[fid] = "new"

        # Seen last time this project was analysed, absent now: the analyzer
        # looked and no longer reports it. Only reachable for projects the
        # current run actually re-analysed, so this cannot fabricate a fix.
        for project_id, prev_run in prev_by_project.items():
            if prev_run not in prev_cache:
                prev_cache[prev_run] = _obs(prev_run)
            for fid in prev_cache[prev_run]:
                if fid in curr_all or owners.get(fid) != project_id:
                    continue
                states[fid] = "resolved"
        return states

    def findings(self, filter: dict | None = None, run_id: str | None = None) -> list[FindingRow]:
        """Findings with R13 delta state derived from the observation ledger.

        `filter` accepts `severity`, `category`, `project_id` (SQL-side) and
        `state` (post-derivation). `state` cannot be pushed into SQL because
        it is computed from two runs' observations, not stored on the row.
        """
        filter = filter or {}
        clauses = []
        params: list[object] = []
        for key in ("severity", "category", "project_id"):
            if filter.get(key):
                clauses.append(f"{key}=?")
                params.append(filter[key])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        rows = self._store.conn.execute(
            f"SELECT id, project_id, category, severity, title, detail, confidence, created_ms, "
            f"resolved_ms, dismissal_reason, provenance, state "
            f"FROM findings {where} ORDER BY created_ms DESC",
            params,
        ).fetchall()
        states = self._delta_states(run_id)
        wanted = filter.get("state")
        wanted_set = set(wanted) if isinstance(wanted, (list, tuple, set)) else (
            {wanted} if wanted else None
        )
        out = []
        for r in rows:
            provenance = "A" if r["provenance"] == "agent" else "D"
            if r["dismissal_reason"]:
                delta_state = "dismissed"
            else:
                delta_state = states.get(r["id"]) or (
                    "resolved" if r["resolved_ms"] else "new"
                )
            if wanted_set is not None and delta_state not in wanted_set:
                continue
            out.append(
                FindingRow(
                    id=r["id"],
                    project_id=r["project_id"],
                    category=r["category"],
                    severity=r["severity"] or "medium",
                    title=r["title"],
                    detail=r["detail"] or "",
                    confidence=r["confidence"] or "medium",
                    created_ms=r["created_ms"],
                    resolved_ms=r["resolved_ms"],
                    dismissal_reason=r["dismissal_reason"],
                    delta_state=delta_state,
                    provenance=provenance,
                )
            )
        return out

    def findings_diff(self, curr_run_id: str | None = None) -> list[FindingDiff]:
        """Classify findings for a run by stable id (R13).

        - new: first observed in the current run
        - persisting: observed in the previous run that covered its project
        - resolved: previously observed, absent or resolved now
        - regressed: previously resolved/dismissed, observed live again

        Delegates to `_delta_states` rather than re-deriving the comparison.
        It briefly did both: this method paired runs globally while the
        findings view paired them per project, so on consecutive runs
        covering different projects the same finding read `persisting` in
        the TUI and `new` under `dreamy diff`. Two classifiers over one
        ledger will always drift, so there is exactly one now.

        The predecessor is not a parameter: it is a property of the ledger
        (the last run that analysed each finding's project), not a caller's
        choice. Accepting one invited exactly the disagreement above.
        """
        states = self._delta_states(curr_run_id)
        if not states:
            return []
        meta = {
            r["id"]: (r["category"], r["severity"] or "medium")
            for r in self._store.conn.execute(
                "SELECT id, category, severity FROM findings"
            ).fetchall()
        }
        out: list[FindingDiff] = []
        for fid in sorted(states):
            category, severity = meta.get(fid, ("unknown", "medium"))
            out.append(
                FindingDiff(
                    finding_id=fid, state=states[fid], category=category, severity=severity
                )
            )
        return out

    def finding_metrics(self) -> dict[str, list[FindingMetric]]:
        """R22 — lifecycle metrics per category and per severity.

        `_delta_states` answers "what is this finding's state NOW". This
        answers what R13 alone cannot: how long findings survive before
        resolution, which buckets regress after being fixed, whether the
        backlog is growing. All of it is derived by replaying `_delta_states`
        once per analysed run — the SAME classifier `findings_diff` uses, for
        the same reason: pairing runs any other way here would silently
        disagree with what `dreamy diff` and the findings view already show.

        A finding whose owning project was never analysed contributes zero
        events to the replay. That is exactly the R21 `not_observed` case:
        no evidence either way, so it is counted separately and excluded
        from every rate/median below rather than folded in as a failure.
        """
        runs = self._store.conn.execute(
            "SELECT DISTINCT a.run_id, r.started_ms FROM run_analyzed_projects a "
            "JOIN runs r ON r.id = a.run_id ORDER BY r.started_ms ASC"
        ).fetchall()

        meta = {
            r["id"]: (r["category"], r["severity"] or "medium", r["created_ms"])
            for r in self._store.conn.execute(
                "SELECT id, category, severity, created_ms FROM findings"
            ).fetchall()
        }
        first_observed = {
            r["finding_id"]: r["first_ms"]
            for r in self._store.conn.execute(
                "SELECT finding_id, MIN(observed_ms) AS first_ms "
                "FROM finding_observations GROUP BY finding_id"
            ).fetchall()
        }

        # One event list per finding, built from the SAME per-project replay
        # `findings_diff` uses — never a second classifier over this ledger.
        timeline: dict[str, list[tuple[int, str]]] = {}
        for run in runs:
            for fid, delta_state in self._delta_states(run["run_id"]).items():
                timeline.setdefault(fid, []).append((run["started_ms"], delta_state))

        now_ms = int(time.time() * 1000)
        open_states = {"new", "persisting", "regressed"}

        def _blank() -> dict:
            return {
                "ttr": [], "resolve_events": 0, "regressed_after_resolve": 0,
                "new_events": 0, "regressed_events": 0, "resolved_events": 0,
                "ages": [],
            }

        acc: dict[str, dict[str, dict]] = {"category": {}, "severity": {}}

        def _acc(dim: str, key: str) -> dict:
            return acc[dim].setdefault(key, _blank())

        for fid in set(meta) - set(timeline):
            category, severity, _created_ms = meta[fid]
            _acc("category", category)["not_observed"] = (
                _acc("category", category).get("not_observed", 0) + 1
            )
            _acc("severity", severity)["not_observed"] = (
                _acc("severity", severity).get("not_observed", 0) + 1
            )

        for fid, events in timeline.items():
            category, severity, created_ms = meta.get(fid, ("unknown", "medium", now_ms))
            cb, sb = _acc("category", category), _acc("severity", severity)
            first_resolve_ms = None
            for i, (started_ms, delta_state) in enumerate(events):
                if delta_state == "new":
                    cb["new_events"] += 1
                    sb["new_events"] += 1
                elif delta_state == "regressed":
                    cb["regressed_events"] += 1
                    sb["regressed_events"] += 1
                elif delta_state == "resolved":
                    cb["resolved_events"] += 1
                    sb["resolved_events"] += 1
                    cb["resolve_events"] += 1
                    sb["resolve_events"] += 1
                    if first_resolve_ms is None:
                        first_resolve_ms = started_ms
                    # Resolved-then-regressed is an EVENT-adjacency check, not
                    # a lifetime flag: a finding resolved three times in this
                    # window contributes up to three independent verdicts,
                    # each judged against the transition that actually
                    # followed it.
                    if i + 1 < len(events) and events[i + 1][1] == "regressed":
                        cb["regressed_after_resolve"] += 1
                        sb["regressed_after_resolve"] += 1
            if first_resolve_ms is not None:
                first_ms = first_observed.get(fid)
                if first_ms is not None:
                    ttr = first_resolve_ms - first_ms
                    cb["ttr"].append(ttr)
                    sb["ttr"].append(ttr)
            # "Currently open" is the LAST event this finding's own project
            # replay produced — not a second global snapshot. Reusing that
            # per-project verdict is what keeps this aligned with
            # `findings()`/`findings_diff()` instead of drifting from them.
            if events[-1][1] in open_states:
                age = now_ms - created_ms
                cb["ages"].append(age)
                sb["ages"].append(age)

        def _percentile(values: list[int], pct: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            if len(ordered) == 1:
                return float(ordered[0])
            idx = (pct / 100) * (len(ordered) - 1)
            lo = int(idx)
            hi = min(lo + 1, len(ordered) - 1)
            frac = idx - lo
            return ordered[lo] + (ordered[hi] - ordered[lo]) * frac

        def _finalize(dim: str) -> list[FindingMetric]:
            keys = set(meta[fid][0 if dim == "category" else 1] for fid in meta)
            out = []
            for key in sorted(keys):
                b = acc[dim].get(key, _blank())
                resolve_events = b["resolve_events"]
                out.append(
                    FindingMetric(
                        key=key,
                        not_observed=b.get("not_observed", 0),
                        resolve_events=resolve_events,
                        time_to_resolve_ms=median(b["ttr"]) if b["ttr"] else None,
                        regression_rate=(
                            round(b["regressed_after_resolve"] / resolve_events, 4)
                            if resolve_events
                            else None
                        ),
                        backlog_delta=(
                            b["new_events"] + b["regressed_events"] - b["resolved_events"]
                        ),
                        open_count=len(b["ages"]),
                        age_p50_ms=_percentile(b["ages"], 50),
                        age_p90_ms=_percentile(b["ages"], 90),
                    )
                )
            return out

        return {"by_category": _finalize("category"), "by_severity": _finalize("severity")}

    # ------- prompts -------
    def prompt_artifacts(
        self, project_id: str | None = None, prompt_type: str | None = None
    ) -> list[PromptRow]:
        """Live prompt artifacts, optionally narrowed by project and/or type.

        Both filters are genuinely optional. Passing `project_id=None` binds
        SQL NULL, which matches no row — so the web route's unfiltered
        `/api/v1/prompts` returned an empty list instead of every prompt. The
        signature claimed `str`, which hid that from the type checker while
        the route passed `None` on every unfiltered request.
        """
        clauses = ["archived_ms IS NULL"]
        args: list[str] = []
        if project_id:
            clauses.append("project_id=?")
            args.append(project_id)
        if prompt_type:
            clauses.append("prompt_type=?")
            args.append(prompt_type)
        rows = self._store.conn.execute(
            "SELECT id, project_id, prompt_type, stable_hash, created_ms FROM prompt_artifacts "
            f"WHERE {' AND '.join(clauses)} ORDER BY created_ms DESC",
            tuple(args),
        ).fetchall()
        return [
            PromptRow(
                id=r["id"],
                project_id=r["project_id"],
                prompt_type=r["prompt_type"],
                stable_hash=r["stable_hash"],
                created_ms=r["created_ms"],
            )
            for r in rows
        ]

    # ------- cost (R10) -------
    def cost_30d(self, project_id: str) -> CostByProject | None:
        row = self._store.conn.execute(
            "SELECT "
            " COALESCE(SUM(rr.cost_usd), 0.0) AS total, "
            " COUNT(*) AS ep_count, "
            " COUNT(DISTINCT rr.linked_session_id) AS uniq_links "
            "FROM router_requests rr "
            "LEFT JOIN sessions s ON s.id = rr.linked_session_id "
            "WHERE s.project_id=?",
            (project_id,),
        ).fetchone()
        if row is None or row["total"] is None:
            return None
        completed = self._store.conn.execute(
            "SELECT COUNT(*) AS c FROM intent_episodes WHERE project_id=? AND completion_status='complete'",
            (project_id,),
        ).fetchone()["c"] or 0
        per_intent = (row["total"] / completed) if completed else 0.0
        tier = self._store.conn.execute(
            "SELECT link_confidence, COUNT(*) AS c FROM router_requests rr "
            "LEFT JOIN sessions s ON s.id = rr.linked_session_id "
            "WHERE s.project_id=? GROUP BY link_confidence",
            (project_id,),
        ).fetchall()
        return CostByProject(
            project_id=project_id,
            total_usd=float(row["total"] or 0.0),
            episode_count=row["ep_count"] or 0,
            per_completed_intent_usd=per_intent,
            confidence_mix={t["link_confidence"] or "unknown": t["c"] for t in tier},
        )

    # ------- topic events (live GUI + 24h replay) -------
    def topic_events(self, since_ms: int, topics: list[str] | None = None, limit: int = 500) -> list[TopicEvent]:
        if self._output_dir is None:
            return []
        log_dir = Path(self._output_dir) / "logs"
        if not log_dir.is_dir():
            return []
        events: list[TopicEvent] = []
        for path in sorted(log_dir.glob("*.jsonl"), reverse=True):
            self._harvest(path, topics, since_ms, events)
            if len(events) >= limit:
                break
        events.sort(key=lambda e: e.ts_ms, reverse=True)
        return events[:limit]

    def _harvest(self, path, topics, since_ms, out):
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = obj.get("ts_ms", 0)
                    if ts < since_ms:
                        continue
                    topic = obj.get("topic", "")
                    if topics and topic not in topics:
                        continue
                    out.append(
                        TopicEvent(
                            ts_ms=ts,
                            topic=topic,
                            level=obj.get("level", "INFO"),
                            msg=obj.get("msg", ""),
                            project_id=obj.get("project_id"),
                            agent_type=obj.get("agent_type"),
                        )
                    )
        except OSError:
            return

    def artifact_content(self, artifact_id: str) -> str | None:
        """Persisted artifact body, for bundles built after retention purge."""
        row = self._store.conn.execute(
            "SELECT content FROM prompt_artifacts WHERE id=? AND archived_ms IS NULL", (artifact_id,)
        ).fetchone()
        return row["content"] if row else None

    # ------- R20 dismissal (reversible) -------
    DISMISSAL_REASONS = ("wrong", "wont-fix", "already-done", "duplicate")

    def dismiss(self, finding_id: str, reason: str) -> bool:
        """Dismiss a finding with a reason code. Reversible via `undismiss`.

        Legal reasons are the four R20 codes; anything else is rejected rather
        than silently stored, because the dismissal-rate metric is only
        meaningful over a closed vocabulary.

        The finding's current evidence digest is pinned at dismissal time so a
        later run can tell "same finding, already judged" from "materially
        changed recurrence" — without that distinction a dismissal either
        suppresses real new evidence or fails to stick at all.
        """
        if reason not in self.DISMISSAL_REASONS:
            raise ValueError(
                f"unknown dismissal reason: {reason!r} "
                f"(expected one of {', '.join(self.DISMISSAL_REASONS)})"
            )
        import time as _time

        from .store import finding_evidence_sha

        row = self._store.conn.execute(
            "SELECT evidence_json FROM findings WHERE id=?", (finding_id,)
        ).fetchone()
        if row is None:
            return False
        self._store.dismiss_finding(
            finding_id,
            reason,
            int(_time.time() * 1000),
            evidence_sha=finding_evidence_sha(row["evidence_json"]),
        )
        self._store.commit()
        return True

    def undismiss(self, finding_id: str) -> bool:
        """Reverse a dismissal (R20 requires dismissal to be reversible)."""
        cur = self._store.conn.execute(
            "UPDATE findings SET resolved_ms=NULL, dismissal_reason=NULL, "
            "dismissed_evidence_sha=NULL, dismissed_ms=NULL WHERE id=?",
            (finding_id,),
        )
        self._store.commit()
        return bool(cur.rowcount)

    # ------- sources (C.3 sources panel) -------
    def source_stats(self) -> list[SourceStat]:
        """Per-source record counts from the latest run.

        `available` answers "did this source yield records in the latest run
        window", NOT "is this harness installed". The two are independent and
        conflating them produced a real UI defect: the dashboard rendered
        `available == False` as "not installed" for claude, codex, opencode,
        and pi while `discover()` reported all four installed with 6136, 84,
        1382, and 1 files respectively. They were simply quiet in an
        incremental window — the honest reading of `error`, which already
        said "no records in window".

        A watermark row is the durable signal: it exists only once a source
        has been successfully ingested, so it survives a window in which the
        source produced nothing. `ever_ingested` carries that, leaving
        `available` to mean what its callers already rely on.

        An absent harness is a NORMAL state rendered dim, never an error —
        colouring it red trains an operator to ignore red.
        """
        latest = self.latest_run()
        counts = dict(latest.source_counts) if latest else {}
        watermarks = {
            r["source_id"]: r["last_ms"]
            for r in self._store.conn.execute(
                "SELECT source_id, last_ms FROM watermark"
            ).fetchall()
        }
        # I9: re-evaluate stored baselines against the live watermark. Read
        # only — computation and persistence happen at ingest via
        # cadence.refresh. A missing row means "never computed", which is
        # distinct from "insufficient data" (computed, sample too small).
        from . import cadence as _cadence

        cadence_rows = {
            r["source_id"]: {
                "source_id": r["source_id"],
                "median_gap_ms": r["median_gap_ms"],
                "sample_size": int(r["sample_size"] or 0),
                "computed_ms": int(r["computed_ms"] or 0),
            }
            for r in self._store.conn.execute(
                "SELECT source_id, median_gap_ms, sample_size, computed_ms "
                "FROM source_cadence"
            ).fetchall()
        }
        now_ms = int(time.time() * 1000)
        out: list[SourceStat] = []
        for source_id in sorted(set(counts) | set(watermarks) | set(cadence_rows)):
            n = int(counts.get(source_id, 0) or 0)
            cadence_label: str | None = None
            row = cadence_rows.get(source_id)
            if row is not None:
                assessment = _cadence.assess_row(
                    source_id,
                    row,
                    watermark_ms=int(watermarks.get(source_id, 0) or 0),
                    now_ms=now_ms,
                )
                cadence_label = assessment.cadence
            out.append(SourceStat(
                source_id=source_id,
                available=n > 0,
                record_count=n,
                path="",
                error=None if n > 0 else "no records in window",
                ever_ingested=source_id in watermarks,
                cadence=cadence_label,
            ))
        return out

    # ------- R10 cost attribution, unattributed bucket included -------
    def cost_rollup(self) -> dict:
        """Per-project cost plus the unattributed bucket.

        Unattributed spend is DISPLAYED, never silently dropped: on real data
        most router requests carry no defensible session link, so hiding that
        bucket would understate total spend by an order of magnitude and make
        the per-project numbers look authoritative when they are partial.
        Cost never ranks or gates anything.
        """
        rows = self._store.conn.execute(
            "SELECT p.id AS project_id, p.name AS name, "
            "       COALESCE(SUM(rr.cost_usd), 0.0) AS total, "
            "       COUNT(rr.id) AS requests "
            "FROM router_requests rr "
            "JOIN sessions s ON s.id = rr.linked_session_id "
            "JOIN projects p ON p.id = s.project_id "
            "WHERE rr.linked_session_id IS NOT NULL "
            "GROUP BY p.id, p.name ORDER BY p.name"
        ).fetchall()
        unattributed = self._store.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total, COUNT(*) AS requests "
            "FROM router_requests WHERE linked_session_id IS NULL"
        ).fetchone()
        tiers = {
            str(r["link_confidence"]): r["n"]
            for r in self._store.conn.execute(
                "SELECT link_confidence, COUNT(*) AS n FROM router_requests "
                "WHERE linked_session_id IS NOT NULL GROUP BY link_confidence"
            ).fetchall()
        }
        attributed_total = sum(float(r["total"] or 0.0) for r in rows)
        return {
            "projects": [
                {
                    "project_id": r["project_id"],
                    "name": r["name"],
                    "total_usd": float(r["total"] or 0.0),
                    "requests": int(r["requests"] or 0),
                }
                for r in rows
            ],
            "attributed_usd": attributed_total,
            "unattributed_usd": float(unattributed["total"] or 0.0) if unattributed else 0.0,
            "unattributed_requests": int(unattributed["requests"] or 0) if unattributed else 0,
            "confidence_mix": tiers,
        }

    # ------- R20 dismissal feedback -------
    def dismissal_rates(self) -> list[dict]:
        """Dismissal rate per category — the real source of the FP metric."""
        rows = self._store.conn.execute(
            "SELECT category, COUNT(*) AS total, "
            "SUM(CASE WHEN dismissal_reason IS NOT NULL THEN 1 ELSE 0 END) AS dismissed "
            "FROM findings GROUP BY category ORDER BY category"
        ).fetchall()
        out = []
        for r in rows:
            total = int(r["total"] or 0)
            dismissed = int(r["dismissed"] or 0)
            out.append({
                "category": r["category"],
                "total": total,
                "dismissed": dismissed,
                "dismissal_rate": round(dismissed / total, 4) if total else 0.0,
            })
        return out

    # ------- schedule state -------
    def schedule_state(self) -> ScheduleState | None:
        """Live schedule truth.

        Config supplies the CONFIGURED interval; `launchctl print` supplies
        what launchd is actually holding. When they disagree the reported
        value is launchd's — a cached calculation that drifts from the loaded
        job is exactly the bug this avoids.
        """
        from . import launchd as _launchd

        try:
            from .config import load_config

            cfg = load_config()
        except Exception:  # noqa: BLE001 — a bad config never breaks the view
            cfg = None
        configured_interval = cfg.interval_seconds if cfg else 21600

        raw = _launchd.print_state()
        installed = _launchd.is_installed()
        live = _launchd.parse_state(raw) if installed else {}
        interval = live.get("interval_seconds") or configured_interval
        next_ms = _launchd.next_run_after(interval) if installed else None
        return ScheduleState(
            label=_launchd.LABEL,
            installed=installed,
            interval_seconds=interval,
            next_run_ms=next_ms,
            plist_path=live.get("plist_path")
            or str(Path.home() / "Library" / "LaunchAgents" / f"{_launchd.LABEL}.plist"),
            last_exit=live.get("last_exit_code"),
            raw=raw,
        )
