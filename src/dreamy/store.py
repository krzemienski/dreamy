"""Canonical schema and SQLite state store with migrations, watermarks, and locking."""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES (1);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    git_remote TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    native_id TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    ended_ms INTEGER,
    model TEXT,
    git_branch TEXT,
    raw_path TEXT,
    record_count INTEGER DEFAULT 0,
    UNIQUE(source_id, native_id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    role TEXT,
    model TEXT,
    content_fingerprint TEXT,
    tool_name TEXT,
    file_paths_json TEXT,
    error_text TEXT,
    raw_meta_json TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS router_requests (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    native_id TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    provider TEXT,
    model TEXT,
    connection_id TEXT,
    status TEXT,
    cost_usd REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    endpoint TEXT,
    linked_session_id TEXT,
    link_confidence TEXT,
    UNIQUE(source_id, native_id)
);

CREATE TABLE IF NOT EXISTS intent_episodes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    ended_ms INTEGER,
    original_intent TEXT,
    acceptance_criteria_json TEXT,
    completion_status TEXT,
    completion_evidence_json TEXT,
    drift_type TEXT,
    pivot_point_ms INTEGER,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT,
    title TEXT NOT NULL,
    detail TEXT,
    evidence_json TEXT,
    confidence TEXT,
    created_ms INTEGER NOT NULL,
    resolved_ms INTEGER,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS prompt_artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    prompt_type TEXT NOT NULL,
    stable_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    archived_ms INTEGER,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS watermark (
    source_id TEXT PRIMARY KEY,
    last_ms INTEGER NOT NULL DEFAULT 0,
    updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_ms INTEGER NOT NULL,
    ended_ms INTEGER,
    status TEXT,
    source_counts_json TEXT,
    warning_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    log_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_router_ts ON router_requests(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_findings_project ON findings(project_id);
CREATE INDEX IF NOT EXISTS idx_episodes_project ON intent_episodes(project_id);
"""

# Numbered migration ladder. Each MIGRATION_Vn list runs in a single
# transaction; the runner records the version in schema_version only after
# every statement in that version succeeds. Idempotent: PRAGMA table_info
# guards each ALTER TABLE so re-running a migration on a partial DB is a no-op.
MIGRATION_V2 = [
    # TASK line 187 + ADR-001: required columns introduced by prompt compiler
    # and structured finding lifecycle.
    "ALTER TABLE findings ADD COLUMN provenance TEXT",
    "ALTER TABLE findings ADD COLUMN state TEXT",
    "ALTER TABLE findings ADD COLUMN dismissal_reason TEXT",
    "ALTER TABLE prompt_artifacts ADD COLUMN chain_json TEXT",
    "ALTER TABLE prompt_artifacts ADD COLUMN health_json TEXT",
    "ALTER TABLE runs ADD COLUMN lookback_days INTEGER",
    "ALTER TABLE sessions ADD COLUMN platform TEXT",
    # Agent SDK observability tables (existing).
    """
    CREATE TABLE IF NOT EXISTS agent_calls (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        agent_type TEXT NOT NULL,
        model TEXT,
        started_ms INTEGER NOT NULL,
        ended_ms INTEGER,
        status TEXT,
        cost_usd REAL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        input_fingerprint TEXT,
        output_json TEXT,
        error_text TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_events (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        agent_type TEXT,
        call_id TEXT,
        ts_ms INTEGER NOT NULL,
        topic TEXT,
        level TEXT,
        message TEXT,
        fields_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_pins (
        skill_name TEXT PRIMARY KEY,
        content_sha256 TEXT NOT NULL,
        pinned_ms INTEGER NOT NULL,
        path TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_calls_run ON agent_calls(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id, ts_ms)",
]

# Migration 003 — router link provenance. `linked_session_id` and
# `link_confidence` shipped in v1, but a reviewer at VG-2 must also see WHICH
# tier fired and WHY, so the reason is auditable without re-running correlation.
MIGRATION_V3 = [
    "ALTER TABLE router_requests ADD COLUMN link_tier INTEGER",
    "ALTER TABLE router_requests ADD COLUMN link_reason TEXT",
    "CREATE INDEX IF NOT EXISTS idx_router_linked ON router_requests(linked_session_id)",
]

# Migration 004 — D24: compiler-version stamp. A `prompt_artifacts` row
# carried no record of which compiler produced it, so a bug fix to the
# skill chain (e.g. D17) could never be detected on rows that were never
# recompiled — they survived every subsequent run untouched and were
# served to a cold operator as current output. See
# acceptance/VG-4/D24-stale-live-tree.md, "Root cause, third pass".
# Nullable: existing rows land NULL, which never equals a computed stamp,
# so every pre-D24 row is stale-by-construction until recompiled or
# explicitly archived.
MIGRATION_V4 = [
    "ALTER TABLE prompt_artifacts ADD COLUMN compiler_version TEXT",
]

# Migration 005 — R13: findings carry no run linkage, so `findings.state`
# was written as the literal 'new' on every upsert and every row in a real
# database reads `new` forever. `findings_diff()` compensated by taking two
# run ids, but `findings()` ignored its own `run_id` parameter, so the diff
# compared a run against itself and every finding classified `new`. Without
# a per-run observation record there is nothing to diff against: a finding
# is not an event, it is a fact that is true across a span of runs.
#
# `finding_observations` is that record — one row per (finding, run) pair,
# so "was this finding present in run N" becomes answerable. Deriving the
# state at read time from observations, rather than storing a state string
# at write time, means the classification cannot go stale: the same two run
# ids always produce the same verdict.
MIGRATION_V5 = [
    """
    CREATE TABLE IF NOT EXISTS finding_observations (
        finding_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        observed_ms INTEGER NOT NULL,
        resolved INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (finding_id, run_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_finding_obs_run ON finding_observations(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_finding_obs_finding ON finding_observations(finding_id)",
    # R20: a dismissal must survive recurrence UNLESS the evidence materially
    # changed. Storing the evidence digest at dismissal time is what makes
    # "materially changed" decidable — without it, "did this come back
    # different?" has no referent and every recurrence looks identical.
    "ALTER TABLE findings ADD COLUMN dismissed_evidence_sha TEXT",
    "ALTER TABLE findings ADD COLUMN dismissed_ms INTEGER",
]

# Migration 006 — R13 completeness. A finding can only be called `resolved`
# if the analyzer actually LOOKED at its project and stopped reporting it.
# Absence of an observation is otherwise ambiguous: the project may simply
# not have been analysed this run (no new sessions in the window, path
# missing, agent disabled). Recording which projects were analysed is what
# separates "checked, gone" from "never checked" — without it a quiet run
# marks every finding in the codebase resolved, which is the most damaging
# possible false signal.
#
# Separate from V5 rather than appended to it: V5 has already been applied
# to live databases, and the ladder only runs versions ABOVE the recorded
# schema_version. A statement appended to an applied migration never
# executes, so the table would be missing on exactly the databases that
# have been running longest.
MIGRATION_V6 = [
    """
    CREATE TABLE IF NOT EXISTS run_analyzed_projects (
        run_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        analyzed_ms INTEGER NOT NULL,
        PRIMARY KEY (run_id, project_id)
    )
    """,
]

# Migration 007 — R14: the bundle needs the git state AS OF the run it ships
# with. It previously shelled out to `git` at export time, which made an
# archival artifact carry live repository state: export a bundle a week after
# a run and the "git state" section describes today's working tree while the
# report and prompts beside it describe the run. That is a second source of
# truth inside a single artifact, and the newer half silently wins.
#
# Persisting the snapshot the analyzer already gathered (run.py gathers it
# per project via `gather_git_evidence`, then discards it) makes the bundle a
# consistent point-in-time record read entirely through `ReadStore`, with no
# subprocess and no dependency on the repo still existing at export time.
MIGRATION_V7 = [
    """
    CREATE TABLE IF NOT EXISTS project_git_snapshots (
        run_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        captured_ms INTEGER NOT NULL,
        status_porcelain TEXT,
        recent_log_json TEXT,
        diff_stat TEXT,
        error TEXT,
        PRIMARY KEY (run_id, project_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_git_snap_project ON project_git_snapshots(project_id, captured_ms)",
]

# I5 — archival import tables.
#
# Version 8 because migrations are allocated in SHIP order, never reserved.
# `_migrate` runs `if current < version`, so a database already at v10 would
# permanently SKIP a v8 added later — the migration would never execute on any
# machine that had upgraded, and the defect would surface as a missing table
# long after release. An earlier draft of this block took v10 to honour a
# planning note that reserved 8 and 9 for I3/I4; that reservation is fiction,
# because a version number only means anything once it has shipped. I3 and I4
# take 9 and 10 in whatever order they land.
#
# These tables are deliberately SEPARATE from `sessions`/`turns`, not an
# extension of them. An imported bundle carries per-bundle opaque ids and
# timestamps only — no project link, no source id, no content, no canonical
# id — so an imported row can never be joined to, deduped against, or mistaken
# for a locally-ingested one. Writing into the live tables would require
# inventing the missing columns, which is how an archive becomes a lie.
MIGRATION_V8 = [
    """
    CREATE TABLE IF NOT EXISTS imported_bundles (
        bundle_id TEXT PRIMARY KEY,
        schema_version INTEGER NOT NULL,
        exported_ms INTEGER NOT NULL,
        imported_ms INTEGER NOT NULL,
        source_file TEXT NOT NULL,
        project_name TEXT,
        session_count INTEGER NOT NULL DEFAULT 0,
        turn_count INTEGER NOT NULL DEFAULT 0,
        -- SHA-256 over the canonical validated payload. Re-import compares
        -- this rather than trusting bundle_id alone: an id collision, or a
        -- tampered re-send reusing a known id, would otherwise be waved
        -- through as "already imported" without the bytes ever being checked.
        payload_sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS imported_sessions (
        bundle_id TEXT NOT NULL,
        export_id TEXT NOT NULL,
        started_ms INTEGER,
        ended_ms INTEGER,
        record_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (bundle_id, export_id),
        FOREIGN KEY (bundle_id) REFERENCES imported_bundles(bundle_id)
    )
    """,
    # The turn FK is COMPOSITE — (bundle_id, session_export_id) — not just
    # bundle_id. Export ids are unique per bundle, never globally, so a
    # bundle-only reference would accept a turn naming a session that exists
    # in some OTHER bundle, or none at all. The composite key is the only form
    # that can express "this turn belongs to that session in this bundle".
    """
    CREATE TABLE IF NOT EXISTS imported_turns (
        bundle_id TEXT NOT NULL,
        export_id TEXT NOT NULL,
        session_export_id TEXT NOT NULL,
        timestamp_ms INTEGER,
        PRIMARY KEY (bundle_id, export_id),
        FOREIGN KEY (bundle_id, session_export_id)
            REFERENCES imported_sessions(bundle_id, export_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_imp_turns_session "
    "ON imported_turns(bundle_id, session_export_id)",
]

# I3 — cumulative spend ledger.
#
# Version 9, not the v8 the planning doc originally reserved: I5 shipped v8
# first (see the comment above MIGRATION_V8), and `_migrate` runs
# `if current < version`, so a database already at v8 would permanently SKIP
# a v8 added here. Versions are allocated in SHIP order, never reserved —
# I3 takes the next free number, 9.
#
# `config.spend_cap_usd` bounds ONE run. At N scheduled runs/day that per-run
# cap composes into an unbounded daily/monthly total: nothing summed spend
# ACROSS runs, so a $5 per-run cap with 4 runs/day permits ~$20/day with no
# component able to see or enforce that total. `spend_ledger` is the record
# that makes a cumulative cap possible — one row per run, written after the
# agent layer completes, so a pre-flight check in a LATER run can sum
# `started_ms` over a trailing window before that run's agent layer starts.
#
# `run_id` is the primary key rather than an autoincrement id: one row per
# run is the invariant, and a duplicate write for the same run_id (a retry,
# or `run_pipeline` invoked twice with an explicit run_id) must upsert the
# same row rather than double-count spend that was already recorded.
#
# Deliberately separate from `runs` rather than adding columns to it: `runs`
# already carries per-run status/counts and is written incrementally
# throughout `run_pipeline` (`start_run` at open, `finish_run` at close).
# The ledger is written ONCE, after the agent layer settles, from the same
# ledger `agent_sdk._sum_run_cost` already reads — so the cumulative check
# and the per-run check share one source of truth for "what did this run
# actually spend", rather than the ledger recomputing it from `agent_calls`
# a second time with room to disagree.
MIGRATION_V9 = [
    """
    CREATE TABLE IF NOT EXISTS spend_ledger (
        run_id      TEXT PRIMARY KEY,
        started_ms  INTEGER NOT NULL,
        cost_usd    REAL NOT NULL DEFAULT 0,
        model       TEXT,
        call_count  INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_spend_started ON spend_ledger(started_ms)",
]

# I9 — connector cadence baselines.
#
# Version 10: next free after I3's spend_ledger took v9. Allocated in SHIP
# order, never reserved — `_migrate` advances with `if current < version`,
# so a database already at v9 would permanently SKIP any lower version
# added here. I9 is the first improvement after I3 to need a table, so it
# takes 10.
#
# `source_cadence` holds the DERIVED baseline for the staleness detector:
# median inter-record gap over the last N ingested timestamps, the sample
# size that median was computed from, and when it was computed. Sample size
# is stored alongside the median so a consumer can tell "quiet source with
# a solid baseline" from "new source whose median is a guess". Staleness
# itself is NOT stored — it is a function of the live watermark and the
# wall clock, re-evaluated on every read.
MIGRATION_V10 = [
    """
    CREATE TABLE IF NOT EXISTS source_cadence (
        source_id      TEXT PRIMARY KEY,
        median_gap_ms  INTEGER,
        sample_size    INTEGER NOT NULL DEFAULT 0,
        computed_ms    INTEGER NOT NULL
    )
    """,
]




CURRENT_SCHEMA_VERSION = 10


class Store:
    def __init__(self, db_path: Path, *, read_only: bool = False):
        self.db_path = db_path
        self.read_only = read_only
        if read_only:
            # R17d: the web surface must not be able to write, migrate, or
            # even create the state file. `mode=ro` makes the connection
            # itself refuse writes at the SQLite layer, and `query_only`
            # closes the loophole where an ATTACHed db would still be
            # writable. Neither is advisory — a write raises rather than
            # silently succeeding. Directory creation, WAL mutation and the
            # migration ladder are all skipped: a reader must never alter
            # the schema a writer owns.
            if not db_path.is_file():
                raise FileNotFoundError(db_path)
            self.conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
            )
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA query_only=ON")
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        result = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            self.conn.close()
            raise RuntimeError(f"dreamy state database integrity check failed: {result}")

    _V1_REQUIRED = (
        "schema_version", "projects", "sessions", "turns", "router_requests",
        "intent_episodes", "findings", "prompt_artifacts", "watermark", "runs",
    )

    # ------- MIGRATION LADDER -------
    #
    # Numbered migrations (MIGRATION_V2, MIGRATION_V3, ...) run sequentially
    # in a single transaction. Each statement is PRAGMA-guarded before exec so
    # a partial DB (already at v2 with some columns) is a clean no-op.
    # schema_version is updated ONLY after the entire batch commits.
    _LADDER = {
        2: MIGRATION_V2,
        3: MIGRATION_V3,
        4: MIGRATION_V4,
        5: MIGRATION_V5,
        6: MIGRATION_V6,
        7: MIGRATION_V7,
        8: MIGRATION_V8,
        9: MIGRATION_V9,
        10: MIGRATION_V10,
        # Future versions append here. Never remove or reorder.
        #
        # Allocate in SHIP order, never by reservation: `_migrate` advances
        # with `if current < version`, so a database that reaches version N
        # will permanently SKIP any version < N added afterwards.
    }
    _ALTER_ADD_COLUMN = re.compile(
        r"^ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)",
        re.IGNORECASE,
    )
    _CREATE_TABLE_IF_NOT_EXISTS = re.compile(
        r"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)",
        re.IGNORECASE,
    )
    _CREATE_INDEX_IF_NOT_EXISTS = re.compile(
        r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)",
        re.IGNORECASE,
    )

    def _table_names(self) -> set[str]:
        return {
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    def _v1_complete(self, tables: set[str] | None = None) -> bool:
        tables = tables if tables is not None else self._table_names()
        return all(t in tables for t in self._V1_REQUIRED)

    def _schema_version(self) -> int:
        row = self.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return row["v"] if row and row["v"] is not None else 0

    def _table_columns(self, table: str) -> set[str]:
        return {
            r["name"]
            for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _guarded_statement(self, stmt: str) -> str | None:
        """Return stmt unchanged, or None if it's an idempotent no-op."""
        m = self._ALTER_ADD_COLUMN.match(stmt.strip())
        if m:
            table, col = m.group(1), m.group(2)
            if table in self._table_names() and col in self._table_columns(table):
                return None  # column already exists
            if table not in self._table_names():
                raise RuntimeError(
                    f"dreamy store migration: cannot ALTER {table}.{col}; table missing"
                )
            return stmt
        if self._CREATE_TABLE_IF_NOT_EXISTS.match(stmt.strip()):
            return stmt  # IF NOT EXISTS — safe
        if self._CREATE_INDEX_IF_NOT_EXISTS.match(stmt.strip()):
            return stmt  # IF NOT EXISTS — safe
        return stmt  # unknown DDL: pass through; SQLite errors loudly on duplicates

    def _run_migration(self, version: int, stmts: list[str]) -> None:
        """Run one migration batch inside a transaction. Updates version row only on success."""
        try:
            self.conn.execute("BEGIN")
            for stmt in stmts:
                guarded = self._guarded_statement(stmt)
                if guarded is None:
                    continue
                self.conn.execute(guarded)
            # Validate expected objects exist before claiming the version.
            tables_after = self._table_names()
            if version >= 2:
                for req_table in ("agent_calls", "agent_events", "skill_pins"):
                    if req_table not in tables_after:
                        raise RuntimeError(
                            f"dreamy store v{version} incomplete: missing {req_table}"
                        )
                for req_table, req_col in (
                    ("findings", "provenance"),
                    ("findings", "state"),
                    ("prompt_artifacts", "chain_json"),
                    ("prompt_artifacts", "health_json"),
                    ("runs", "lookback_days"),
                    ("sessions", "platform"),
                ):
                    if req_table not in tables_after or req_col not in self._table_columns(req_table):
                        raise RuntimeError(
                            f"dreamy store v{version} incomplete: missing {req_table}.{req_col}"
                        )
            if version >= 4:
                # D24: the compiler-version stamp is what lets a stale row be
                # identified without ever being recompiled. Gated on its own
                # `>= 4` check (not folded into the v2 block above) so that
                # applying v2/v3 on a bare v1 DB does not fail waiting for a
                # column that v4's own ALTER has not run yet.
                if (
                    "prompt_artifacts" not in tables_after
                    or "compiler_version" not in self._table_columns("prompt_artifacts")
                ):
                    raise RuntimeError(
                        f"dreamy store v{version} incomplete: missing prompt_artifacts.compiler_version"
                    )
            if version >= 8:
                # I5 archival import. Validated like every earlier version:
                # a migration must not claim its number on a partial schema,
                # because the importer would then fail later with a missing
                # table while `schema_version` insisted the migration ran.
                for req_table in ("imported_bundles", "imported_sessions", "imported_turns"):
                    if req_table not in tables_after:
                        raise RuntimeError(
                            f"dreamy store v{version} incomplete: missing {req_table}"
                        )
                if "payload_sha256" not in self._table_columns("imported_bundles"):
                    raise RuntimeError(
                        f"dreamy store v{version} incomplete: "
                        "missing imported_bundles.payload_sha256"
                    )
                idx_names = {
                    r["name"]
                    for r in self.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    ).fetchall()
                }
                if "idx_imp_turns_session" not in idx_names:
                    raise RuntimeError(
                        f"dreamy store v{version} incomplete: missing idx_imp_turns_session"
                    )
                # The COMPOSITE turn FK is the constraint that keeps an
                # imported turn attached to a session in its OWN bundle.
                # Export ids are unique per bundle, never globally, so a
                # bundle-only FK would silently permit cross-bundle orphans.
                # Assert its shape, not merely that some FK exists.
                # Grouped by FK id, NOT flattened into a set of columns.
                # `PRAGMA foreign_key_list` returns one row per column, and a
                # set of (table, from, to) triples cannot distinguish ONE
                # composite FK from TWO independent single-column FKs.
                # Measured: the correct schema yields 1 group of size 2, while
                # `FOREIGN KEY(bundle_id)... , FOREIGN KEY(session_export_id)...`
                # yields 2 groups of size 1 — and only the composite form
                # actually constrains a turn to a session in its OWN bundle.
                def _fk_groups(table: str) -> list[list[tuple[str, str, str]]]:
                    rows = self.conn.execute(
                        f"PRAGMA foreign_key_list({table})"
                    ).fetchall()
                    grouped: dict[int, list] = {}
                    for r in rows:
                        grouped.setdefault(r["id"], []).append(r)
                    return [
                        [(x["table"], x["from"], x["to"]) for x in sorted(g, key=lambda y: y["seq"])]
                        for g in grouped.values()
                    ]

                want_turns = [
                    ("imported_sessions", "bundle_id", "bundle_id"),
                    ("imported_sessions", "session_export_id", "export_id"),
                ]
                if want_turns not in _fk_groups("imported_turns"):
                    raise RuntimeError(
                        f"dreamy store v{version} incomplete: imported_turns lacks a single "
                        f"COMPOSITE (bundle_id, session_export_id) foreign key into "
                        f"imported_sessions; found {_fk_groups('imported_turns')}"
                    )
                want_sessions = [("imported_bundles", "bundle_id", "bundle_id")]
                if want_sessions not in _fk_groups("imported_sessions"):
                    raise RuntimeError(
                        f"dreamy store v{version} incomplete: imported_sessions lacks its "
                        f"foreign key into imported_bundles; found {_fk_groups('imported_sessions')}"
                    )
            if version >= 9:
                # I3 cumulative spend ledger. Validated like every earlier
                # version: a migration must not claim its number on a partial
                # schema, because `run_pipeline`'s post-agent-layer write
                # would then fail later with a missing table while
                # `schema_version` insisted the migration ran.
                if "spend_ledger" not in tables_after:
                    raise RuntimeError(
                        f"dreamy store v{version} incomplete: missing spend_ledger"
                    )
                idx_names = {
                    r["name"]
                    for r in self.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    ).fetchall()
                }
                if "idx_spend_started" not in idx_names:
                    raise RuntimeError(
                        f"dreamy store v{version} incomplete: missing idx_spend_started"
                    )
            if version >= 10:
                # I9 connector cadence. A migration must not claim its
                # number on a partial schema: `cadence.refresh` would then
                # fail later with a missing table while `schema_version`
                # insisted the migration ran.
                if "source_cadence" not in tables_after:
                    raise RuntimeError(
                        f"dreamy store v{version} incomplete: missing source_cadence"
                    )
                for req_col in ("source_id", "median_gap_ms", "sample_size", "computed_ms"):
                    if req_col not in self._table_columns("source_cadence"):
                        raise RuntimeError(
                            f"dreamy store v{version} incomplete: "
                            f"missing source_cadence.{req_col}"
                        )
            # Singular contract: collapse to current version inside the same
            # transaction. schema_version holds exactly one row whose value is
            # the current schema version.
            self.conn.execute("DELETE FROM schema_version WHERE version != ?", (version,))
            self.conn.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (version,))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _migrate(self) -> None:
        # Bootstrap v1 if no schema yet.
        tables = self._table_names()
        if "schema_version" not in tables or not self._v1_complete(tables):
            self.conn.executescript(SCHEMA_V1)  # creates schema_version with row(1)
            self.conn.commit()
            tables = self._table_names()

        if not self._v1_complete(tables):
            missing = [t for t in self._V1_REQUIRED if t not in tables]
            raise RuntimeError(
                f"dreamy store migration failed: missing v1 tables {missing} in {self.db_path}"
            )

        current = self._schema_version()
        for version in sorted(self._LADDER.keys()):
            if current < version:
                self._run_migration(version, self._LADDER[version])
                current = version

    def get_watermark(self, source_id: str) -> int:
        row = self.conn.execute("SELECT last_ms FROM watermark WHERE source_id=?", (source_id,)).fetchone()
        return row["last_ms"] if row else 0

    def set_watermark(self, source_id: str, ms: int):
        self.conn.execute(
            "INSERT INTO watermark(source_id, last_ms, updated_ms) VALUES(?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET last_ms=excluded.last_ms, updated_ms=excluded.updated_ms",
            (source_id, ms, int(time.time() * 1000))
        )

    def cadence_timestamps(self, source_id: str, limit: int) -> list[int]:
        """Persisted event timestamps for one source, newest first.

        I9 computes cadence from durable history, never from a single run's
        scan batch — an incremental run landing one record would otherwise
        overwrite a valid 200-record baseline with `insufficient`, erasing the
        staleness detector's input exactly when it is needed.

        Records do not land in one table. Session-level records become
        `turns`; router usage becomes `router_requests`; both are canonical.
        Querying only `turns` means a router-only source yields an EMPTY
        cadence despite ingesting records — the bias is silent, and the
        detector reports a healthy source as unknown. UNION both so a source
        populated by either path has a baseline.

        Returns newest-first because the cadence baseline is the recent
        rhythm, not the all-time average: a source that ran hourly last week
        and daily this week is a daily source now.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT ts FROM ("
            "  SELECT timestamp_ms AS ts FROM turns WHERE source_id=? "
            "  UNION ALL "
            "  SELECT timestamp_ms AS ts FROM router_requests WHERE source_id=? "
            ") ORDER BY ts DESC LIMIT ?",
            (source_id, source_id, int(limit)),
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    def upsert_project(self, path: str, name: str, first_ms: int, last_ms: int, git_remote: str = "") -> str:
        # SAFE-002: `git_remote` is read from git config and can carry
        # credentials in the URL (`https://user:TOKEN@host/repo.git`).
        #
        # `path` is deliberately NOT redacted. It is the project's identity: it
        # derives `pid`, is joined against by every other table, and is compared
        # to on-disk paths during correlation. Redacting it would break those
        # joins for exactly the projects whose paths matched a secret pattern.
        # A filesystem path is a weaker disclosure than a credential, and this
        # is a local store — but it IS a disclosure, recorded rather than
        # silently accepted.
        from .redact import redact_text

        name = redact_text(name)
        git_remote = redact_text(git_remote)

        pid = stable_id("project", path)
        self.conn.execute(
            "INSERT INTO projects(id,path,name,first_seen_ms,last_seen_ms,git_remote) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET last_seen_ms=excluded.last_seen_ms, "
            "git_remote=COALESCE(NULLIF(excluded.git_remote,''),git_remote)",
            (pid, path, name, first_ms, last_ms, git_remote)
        )
        return pid

    def upsert_session(
        self, project_id, source_id, native_id, started_ms, ended_ms, model,
        git_branch, raw_path, record_count,
    ):
        # SAFE-002: `model`, `git_branch`, and `raw_path` are read from the
        # harness transcript. `project_id`/`source_id`/`native_id` are
        # identifiers and feed `sid`, so they are left intact.
        from .redact import redact_text

        model = redact_text(model)
        git_branch = redact_text(git_branch)
        raw_path = redact_text(raw_path)

        sid = stable_id("session", source_id, native_id)
        self.conn.execute(
            "INSERT INTO sessions(id,project_id,source_id,native_id,started_ms,ended_ms,model,"
            "git_branch,raw_path,record_count) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET ended_ms=COALESCE(excluded.ended_ms,ended_ms), "
            "record_count=excluded.record_count",
            (sid, project_id, source_id, native_id, started_ms, ended_ms, model, git_branch, raw_path, record_count)
        )
        return sid

    def insert_turn(
        self, session_id, source_id, ts_ms, role, model, fingerprint,
        tool_name, file_paths_json, error_text, raw_meta_json,
    ) -> int:
        """Insert a turn. Returns 1 if a row was inserted, 0 if the content-derived
        id already existed (idempotent re-run).

        SAFE-002: every free-text column here is transcript-derived — this is
        the highest-volume untrusted path in the system, one row per turn of
        every ingested session. `error_text` and `raw_meta_json` quote model and
        tool output directly; `file_paths_json` carries paths a tool call
        chose; `model` and `tool_name` are strings the harness wrote, not
        values Dreamy computes.

        `session_id`, `source_id`, and `fingerprint` are NOT redacted: they are
        identifiers, and `tid` is derived from `session_id`, `role`,
        `tool_name`, and `fingerprint`. Redaction happens BEFORE that
        derivation so the id is stable against a secret appearing in
        `tool_name` — otherwise the same turn would key differently depending
        on whether it happened to contain a credential.
        """
        from .redact import redact_text

        role = redact_text(role)
        model = redact_text(model)
        tool_name = redact_text(tool_name)
        file_paths_json = redact_text(file_paths_json)
        error_text = redact_text(error_text)
        raw_meta_json = redact_text(raw_meta_json)

        tid = stable_id("turn", session_id, str(ts_ms), role or "", (tool_name or ""), fingerprint or "")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO turns(id,session_id,source_id,timestamp_ms,role,model,"
            "content_fingerprint,tool_name,file_paths_json,error_text,raw_meta_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tid, session_id, source_id, ts_ms, role, model, fingerprint, tool_name, file_paths_json,
             error_text, raw_meta_json)
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def insert_router(
        self, source_id, native_id, ts_ms, provider, model, conn_id,
        status, cost, ptoks, ctoks, endpoint,
    ):
        """Persist one router request.

        SAFE-002: every free-text column here is copied from an external
        router-log row, not computed by Dreamy. `endpoint` is the obvious
        carrier — a URL can hold userinfo (`https://user:TOKEN@host/...`) or a
        query-string key — but `provider`, `model`, `connection_id`, and
        `status` come from the same untrusted row and are redacted for the same
        reason. Redacting only the field with a known exploit shape would leave
        the others open to the next one.

        `source_id` and `native_id` are deliberately NOT redacted: they are
        identifiers, and `rid` is derived from them. `link_router_request`
        recomputes that id from the same two values, so redacting them here
        would break correlation whenever a value happened to match a secret
        pattern — silently, and only for the affected rows.
        """
        from .redact import redact_text

        # `redact_text`, not `redact`: these values are copied from an external
        # row, so their types are not guaranteed. A SQLite-backed source can
        # return a BLOB, whose bytes SQLite would store verbatim; a malformed
        # log can carry a numeric `status`, which `redact()` would raise on.
        provider = redact_text(provider)
        model = redact_text(model)
        conn_id = redact_text(conn_id)
        status = redact_text(status)
        endpoint = redact_text(endpoint)

        rid = stable_id("router", source_id, native_id)
        # Upsert on the stable id rather than INSERT OR REPLACE: REPLACE deletes
        # the existing row first, which would silently discard the correlation
        # link columns written by a previous run's correlate phase.
        self.conn.execute(
            "INSERT INTO router_requests(id,source_id,native_id,timestamp_ms,provider,model,"
            "connection_id,status,cost_usd,prompt_tokens,completion_tokens,endpoint) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "timestamp_ms=excluded.timestamp_ms, provider=excluded.provider, model=excluded.model, "
            "connection_id=excluded.connection_id, status=excluded.status, cost_usd=excluded.cost_usd, "
            "prompt_tokens=excluded.prompt_tokens, completion_tokens=excluded.completion_tokens, "
            "endpoint=excluded.endpoint",
            (rid, source_id, native_id, ts_ms, provider, model, conn_id, status, cost, ptoks, ctoks, endpoint)
        )

    def link_router_request(self, source_id: str, native_id: str, session_id: str,
                            tier: int, confidence: str, reason: str) -> int:
        """Attach a correlation link to one router request. Idempotent.

        Returns 1 when a row was updated, 0 when the request is not present
        (e.g. outside the ingest window) — never raises on a missing row.
        """
        # SAFE-002: `reason` quotes the evidence that justified the link,
        # which can include transcript text. `confidence` is a controlled
        # vocabulary but arrives as a plain string, so it is cleaned too.
        from .redact import redact_text

        confidence = redact_text(confidence)
        reason = redact_text(reason)

        rid = stable_id("router", source_id, native_id)
        cur = self.conn.execute(
            "UPDATE router_requests SET linked_session_id=?, link_confidence=?, "
            "link_tier=?, link_reason=? WHERE id=?",
            (session_id, confidence, int(tier), reason, rid),
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def recover_interrupted_runs(self, now_ms: int) -> int:
        """Close run rows orphaned by a crash or kill. Returns rows recovered.

        A run row is opened `running` and only updated at completion, so a
        process killed mid-run (SIGKILL, launchd bootout, power loss) leaves
        the row `running` forever. Nothing reconciled them: measured on this
        machine, three orphans accumulated and two later successful runs left
        them untouched, so `status='running'` could never be trusted to mean
        "a run is happening now".

        MUST be called only while the caller holds the exclusive `RunLock`,
        and before the new run's row is inserted. The lock is what makes the
        blanket predicate safe: no other run can be legitimately `running`
        while we hold it, so every such row is by definition abandoned.
        Calling it without the lock would mark a live sibling run interrupted.

        `ended_ms` is stamped with the recovery time rather than the original
        start: the run genuinely occupied the machine until something ended
        it, and back-dating would report a zero-length run that never happened.
        `started_ms`, `lookback_days`, and the id are preserved untouched.
        """
        cur = self.conn.execute(
            "UPDATE runs SET status='interrupted', ended_ms=? "
            "WHERE status='running' AND ended_ms IS NULL",
            (int(now_ms),),
        )
        return cur.rowcount or 0

    def start_run(self, run_id: str, started_ms: int, lookback_days: int | None = None):
        """Open a run row, recording the window it was reconciled over (R11.b).

        `lookback_days` is stored so a report can state its own window rather
        than implying the config default it may not have used.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO runs(id, started_ms, status, lookback_days) VALUES(?,?,?,?)",
            (run_id, started_ms, "running", int(lookback_days) if lookback_days else None)
        )

    def upsert_git_snapshot(self, run_id: str, project_id: str, evidence, captured_ms: int) -> None:
        """Persist the git state observed for one project during one run (R14).

        SAFE-002: every field originates from the project's own repository —
        commit subjects, author names and file paths — so all of it passes
        through `redact()` before it lands, exactly as session content does.
        """
        import json as _json

        from .redact import redact_text

        recent_log = getattr(evidence, "recent_log", None) or []
        self.conn.execute(
            "INSERT INTO project_git_snapshots(run_id, project_id, captured_ms, "
            "status_porcelain, recent_log_json, diff_stat, error) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id, project_id) DO UPDATE SET "
            "captured_ms=excluded.captured_ms, status_porcelain=excluded.status_porcelain, "
            "recent_log_json=excluded.recent_log_json, diff_stat=excluded.diff_stat, "
            "error=excluded.error",
            (
                run_id,
                project_id,
                int(captured_ms),
                redact_text(getattr(evidence, "status_porcelain", "") or ""),
                redact_text(_json.dumps(recent_log, default=str)),
                redact_text(getattr(evidence, "diff_stat", "") or ""),
                redact_text(getattr(evidence, "error", None)),
            ),
        )

    def finish_run(self, run_id: str, ended_ms: int, status: str, source_counts_json: str | None = None,
                    warning_count: int = 0, error_count: int = 0, log_path: str | None = None):
        """Close out a run.

        SAFE-002: `source_counts_json` aggregates per-source detail that can
        quote a source's own error strings, and `log_path` is a filesystem
        path. `status` is a controlled vocabulary but arrives as a plain
        string. `run_id` is an identifier and is left intact.
        """
        from .redact import redact_text

        status = redact_text(status)
        source_counts_json = redact_text(source_counts_json)
        log_path = redact_text(log_path)

        self.conn.execute(
            "UPDATE runs SET ended_ms=?, status=?, source_counts_json=?, warning_count=?, "
            "error_count=?, log_path=? WHERE id=?",
            (ended_ms, status, source_counts_json, warning_count, error_count, log_path, run_id)
        )

    def insert_agent_call(
        self, run_id: str | None, agent_type: str, started_ms: int,
        model: str | None = None, input_fingerprint: str | None = None,
    ) -> str:
        """Open a ledger row for one paid agent call. Returns its id.

        The id is RANDOM, not content-derived. It was
        `stable_id(run_id, agent_type, started_ms, input_fingerprint)`, which
        collides whenever the same prompt is issued twice inside one
        millisecond — a retry, or two identical calls in a tight loop. That
        raised `IntegrityError` out of `call_claude`, whose contract says it
        never raises on model failure.

        Deduplicating instead would be worse: each of those calls really was
        billed, so folding them into one row undercounts spend, and the spend
        cap reads exactly that sum. A cap that under-measures is not a cap.

        `input_fingerprint` is still stored, so identical prompts remain
        groupable — the property the derived id was reaching for, without
        making it the primary key.

        SAFE-002: `model` is whatever the SDK reported. `agent_type` is a
        Dreamy-internal role name and `input_fingerprint` is a digest.
        """
        from .redact import redact_text

        model = redact_text(model)

        call_id = f"agent_call-{uuid.uuid4().hex}"
        self.conn.execute(
            "INSERT INTO agent_calls(id,run_id,agent_type,model,started_ms,input_fingerprint) VALUES(?,?,?,?,?,?)",
            (call_id, run_id, agent_type, model, started_ms, input_fingerprint)
        )
        return call_id

    def finish_agent_call(
        self, call_id: str, ended_ms: int, status: str, cost_usd: float | None = None,
        tokens: tuple[int | None, int | None] | None = None,
        output_json: str | None = None, error_text: str | None = None,
    ):
        """Persist an agent call's outcome.

        SAFE-002: both free-text columns are redacted HERE rather than at the
        call sites. `output_json` carries model-controlled content and
        `error_text` interpolates exception strings that can echo prompt
        material, so a caller that forgets to redact would write secrets to
        SQLite. One boundary cannot be forgotten; five call sites can.
        """
        from .redact import redact_text

        status = redact_text(status)
        output_json = redact_text(output_json)
        error_text = redact_text(error_text)
        prompt_tokens, completion_tokens = tokens if tokens else (None, None)
        self.conn.execute(
            "UPDATE agent_calls SET ended_ms=?, status=?, cost_usd=?, prompt_tokens=?, "
            "completion_tokens=?, output_json=?, error_text=? WHERE id=?",
            (ended_ms, status, cost_usd, prompt_tokens, completion_tokens, output_json, error_text, call_id)
        )

    def insert_agent_event(self, run_id, agent_type, call_id, ts_ms, topic, level, message, fields_json=None) -> str:
        """Persist one agent-timeline event.

        SAFE-002: `message` and `fields_json` are model- and exception-derived,
        so they are redacted HERE for the same reason `finish_agent_call`
        redacts its two free-text columns — one boundary cannot be forgotten,
        every call site can. The event id is computed from the REDACTED message
        so that two events differing only in secret material collide rather
        than silently producing two rows keyed by unredacted text.
        """
        from .redact import redact_text

        agent_type = redact_text(agent_type)
        topic = redact_text(topic)
        level = redact_text(level)
        message = redact_text(message)
        fields_json = redact_text(fields_json)
        event_id = stable_id("agent_event", run_id or "", call_id or "", str(ts_ms), topic or "", message or "")
        self.conn.execute(
            "INSERT INTO agent_events(id,run_id,agent_type,call_id,ts_ms,topic,level,message,"
            "fields_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, run_id, agent_type, call_id, ts_ms, topic, level, message, fields_json)
        )
        return event_id

    def record_run_spend(
        self, run_id: str, started_ms: int, cost_usd: float, model: str | None,
        call_count: int,
    ) -> None:
        """Write this run's spend_ledger row (I3). Called ONCE, after the
        agent layer settles, from `run_pipeline` — never mid-run.

        `INSERT ... ON CONFLICT` rather than plain `INSERT`: `run_id` is the
        primary key, and a retry that calls `run_pipeline` twice with an
        explicit `run_id` must overwrite the same row with the final total,
        not raise `IntegrityError` or silently double the ledger via a
        second row.

        `model` is whatever the SDK reported for the run's most recent paid
        call — informational only, the cumulative-cap arithmetic never reads
        it. Redacted like every other SDK-derived free-text column so a
        model string that happens to echo prompt material never lands
        unredacted.
        """
        from .redact import redact_text

        model = redact_text(model)

        self.conn.execute(
            "INSERT INTO spend_ledger(run_id, started_ms, cost_usd, model, call_count) "
            "VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
            "started_ms=excluded.started_ms, cost_usd=excluded.cost_usd, "
            "model=excluded.model, call_count=excluded.call_count",
            (run_id, int(started_ms), float(cost_usd), model, int(call_count)),
        )

    def spend_since(self, since_ms: int) -> float:
        """Sum `spend_ledger.cost_usd` for runs started at or after `since_ms`
        (I3). The single query both the daily and monthly pre-flight checks
        run, parameterised by window start — same source of truth for both,
        so a daily and a monthly cap can never disagree about what one run
        cost.
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_ledger "
            "WHERE started_ms >= ?",
            (int(since_ms),),
        ).fetchone()
        return float(row["total"] or 0.0) if row else 0.0

    def upsert_skill_pin(self, name: str, sha: str, path: str | None = None):
        # SAFE-002: `path` is a filesystem location and `name` comes from
        # plugin metadata; `sha` is a digest and is left intact.
        from .redact import redact_text

        name = redact_text(name)
        path = redact_text(path)

        self.conn.execute(
            "INSERT INTO skill_pins(skill_name,content_sha256,pinned_ms,path) VALUES(?,?,?,?) "
            "ON CONFLICT(skill_name) DO UPDATE SET "
            "content_sha256=excluded.content_sha256, "
            "pinned_ms=excluded.pinned_ms, path=excluded.path",
            (name, sha, now_ms(), path)
        )

    def record_finding_observation(
        self, finding_id: str, run_id: str, observed_ms: int, resolved: bool = False
    ) -> None:
        """Record that `finding_id` was present in `run_id` (R13).

        This is what makes diff-since-last-run answerable. A finding is a
        fact that holds across a span of runs, not an event, so "is this
        new?" can only be decided against the set of runs that observed it.
        Idempotent per (finding, run) so a re-run never double-counts.
        """
        self.conn.execute(
            "INSERT INTO finding_observations(finding_id, run_id, observed_ms, resolved) "
            "VALUES(?,?,?,?) ON CONFLICT(finding_id, run_id) DO UPDATE SET "
            "observed_ms=excluded.observed_ms, resolved=excluded.resolved",
            (finding_id, run_id, observed_ms, 1 if resolved else 0),
        )

    def record_analyzed_project(self, run_id: str, project_id: str, analyzed_ms: int) -> None:
        """Record that `run_id` actually analysed `project_id` (R13).

        Read by the diff to decide whether a missing observation means
        "resolved" or "not looked at". Idempotent per (run, project).
        """
        self.conn.execute(
            "INSERT INTO run_analyzed_projects(run_id, project_id, analyzed_ms) "
            "VALUES(?,?,?) ON CONFLICT(run_id, project_id) DO UPDATE SET "
            "analyzed_ms=excluded.analyzed_ms",
            (run_id, project_id, analyzed_ms),
        )

    def dismiss_finding(
        self,
        finding_id: str,
        reason: str,
        resolved_ms: int,
        evidence_sha: str | None = None,
    ):
        # SAFE-002: `reason` is operator-supplied free text and can quote a
        # transcript excerpt explaining the dismissal.
        from .redact import redact_text

        reason = redact_text(reason)

        # R20: pin the evidence digest the operator actually judged. A later
        # run whose evidence hashes the same is the SAME finding and stays
        # dismissed; one that hashes differently is a materially changed
        # recurrence and is allowed back as `regressed`. Without this the
        # two cases are indistinguishable and one of them must be wrong.
        if evidence_sha is None:
            row = self.conn.execute(
                "SELECT evidence_json FROM findings WHERE id=?", (finding_id,)
            ).fetchone()
            evidence_sha = finding_evidence_sha(row["evidence_json"] if row else None)

        self.conn.execute(
            "UPDATE findings SET dismissal_reason=?, resolved_ms=?, "
            "dismissed_evidence_sha=?, dismissed_ms=? WHERE id=?",
            (reason, resolved_ms, evidence_sha, resolved_ms, finding_id),
        )

    def insert_finding_with_provenance(
        self,
        project_id: str,
        finding: dict,
        *,
        provenance: str,
        created_ms: int | None = None,
        run_id: str | None = None,
    ) -> str:
        """Upsert a single finding into `findings` with `provenance`
        enforced at write time. The single entry point for any path that
        needs to write findings — deterministic callers pass
        `provenance='deterministic'`, paid-agent callers pass
        `provenance='agent'`. Cross-provenance writes are a bug.
        Returns the stable finding id.

        The provenance value MUST be one of {'deterministic', 'agent'};
        any other value raises so we cannot accidentally persist 'agent'
        rows for paid calls that never actually ran.
        """
        import json

        from .redact import redact, redact_text

        if provenance not in ("deterministic", "agent"):
            raise ValueError(
                f"insert_finding_with_provenance: provenance must be "
                f"'deterministic' or 'agent', got {provenance!r}"
            )

        # SAFE-002: `title`, `detail`, and `evidence` are model-authored on the
        # `provenance='agent'` path and quote transcript material on both, so
        # they are redacted at this boundary rather than at each caller.
        # Redaction happens BEFORE the id is derived: deriving from raw text
        # would key rows by secret material and let two findings differing only
        # in a leaked credential persist as distinct rows.
        category = redact_text(finding.get("category", "research_support"))
        severity = redact_text(finding.get("severity", "medium"))
        confidence = redact_text(finding.get("confidence", "medium"))
        state = redact_text(finding.get("state", "new"))
        title = redact_text(finding.get("title", ""))
        detail = redact_text(finding.get("detail", ""))
        evidence_json = redact(json.dumps(finding.get("evidence", {}), default=str))
        evidence_key = redact(
            json.dumps(finding.get("evidence", {}), sort_keys=True, default=str)
        )

        fid = stable_id(
            "finding",
            project_id,
            title,
            category,
            provenance,
            evidence_key,
        )
        self.conn.execute(
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
                category,
                severity,
                title,
                detail,
                evidence_json,
                confidence,
                int(created_ms) if created_ms is not None else now_ms(),
                provenance,
                state,
            ),
        )
        # R13: record the sighting so diff-since-last-run has something to
        # compare against. Callers that omit `run_id` (tests, ad-hoc repair
        # scripts) simply produce no observation rather than a wrong one.
        if run_id:
            self.record_finding_observation(
                fid,
                run_id,
                int(created_ms) if created_ms is not None else now_ms(),
                resolved=False,
            )
        return fid

    def commit(self):
        self.conn.commit()

    def close(self, *, discard: bool = False):
        """Close the connection, refusing to silently discard pending writes.

        `sqlite3` opens a transaction on the first write and holds it until
        `commit()`. Closing instead throws the whole transaction away with no
        error and no log line — the writes simply never happened.

        Not hypothetical: seeding a small run/finding/observation fixture for
        a documentation example, then calling `close()`, produced a completely
        empty report. Nothing indicated a problem.
        The production pipeline is unaffected because `run_pipeline` commits
        explicitly, which is exactly what makes this dangerous: the defect is
        invisible in the path that matters and waits for every other caller.

        Committing on close was rejected — it turns an abandoned half-write
        into a durable one, which is worse than losing it. Rolling back
        silently was rejected for being the same silence with the opposite
        outcome. Raising makes the caller state intent.

        No pending-statement COUNT is reported: `sqlite3` exposes
        `in_transaction` (a boolean) and `total_changes` (cumulative for the
        connection, including already-committed rows). Neither is a pending
        count, and deriving one from `total_changes` would print a number
        that is wrong for any caller that committed earlier in its life.
        """
        if discard:
            # Deliberate teardown: the caller has said the writes are
            # unwanted, so there is nothing to warn about.
            try:
                self.conn.rollback()
            finally:
                self.conn.close()
            return

        if not self.conn.in_transaction:
            self.conn.close()
            return

        # The common call shape is `try: ... finally: store.close()`. Raising
        # unconditionally would replace the caller's in-flight exception with
        # this one and hide the actual cause — a debugging obstacle dressed as
        # a debugging aid. Under an active exception the pending writes were
        # never going to be committed anyway, so roll back and leave the
        # original traceback intact.
        if sys.exc_info()[0] is not None:
            try:
                self.conn.rollback()
            finally:
                self.conn.close()
            return

        self.conn.rollback()
        self.conn.close()
        raise RuntimeError(
            "Store.close() called with uncommitted writes; they were discarded. "
            "Call commit() to keep them, or close(discard=True) to drop them "
            "deliberately."
        )


def stable_id(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def finding_evidence_sha(evidence_json: str | None) -> str:
    """Digest of a finding's evidence payload (R20 material-change test).

    Keys are re-sorted before hashing so a serialization-order change is not
    mistaken for a material change — otherwise every dict reordering would
    resurrect dismissed findings and make dismissal useless in practice.
    """
    import json as _json

    if not evidence_json:
        return hashlib.sha256(b"").hexdigest()[:16]
    try:
        parsed = _json.loads(evidence_json)
        canonical = _json.dumps(parsed, sort_keys=True, default=str)
    except (ValueError, TypeError):
        canonical = str(evidence_json)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_to_ms(s: str) -> int:
    """Parse ISO timestamp string to epoch milliseconds."""
    if not s:
        return 0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


class RunLock:
    """Single-instance file lock using fcntl."""
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self.fd, 0)
            os.write(self.fd, str(os.getpid()).encode())
            return True
        except OSError:
            os.close(self.fd)
            self.fd = None
            return False

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
            try:
                self.lock_path.unlink()
            except OSError:
                pass
