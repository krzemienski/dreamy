"""Self-check for correlate.persist_correlated against REAL harness sources.

Exercises the production function (the same one run_pipeline calls) and
asserts:
  1. canonical tables projects/sessions/turns are non-empty
  2. every turn maps to a real session row (no orphan turns)
  3. every session maps to a real project row (no dangling project_id,
     incl. "")
  4. a second persist over identical records adds ZERO rows (idempotency,
     VG-1)

A frozen snapshot of every source is taken first so concurrent live writes
cannot fake or break the idempotency result.

This exercises every configured live connector source (Claude, Codex,
opencode, 9router, ...), some of which hold gigabytes of session history.
Snapshotting them is real disk I/O against real data, so this test is
opt-in: it is skipped unless `DREAMY_RUN_ACCEPTANCE=1` is set in the
environment, in addition to `@pytest.mark.acceptance`.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from dreamy.config import Config
from dreamy.correlate import correlate_records, persist_correlated
from dreamy.ingest import ingest
from dreamy.store import Store

pytestmark = pytest.mark.acceptance

skip_reason = (
    "heavy live-source snapshot + ingest; set DREAMY_RUN_ACCEPTANCE=1 to opt in"
)


def _counts(store: Store) -> dict[str, int]:
    cur = store.conn.cursor()
    return {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("projects", "sessions", "turns")}


@pytest.mark.skipif(
    os.environ.get("DREAMY_RUN_ACCEPTANCE") != "1", reason=skip_reason
)
def test_persist_correlated_is_consistent_and_idempotent(tmp_path: Path, frozen_source_overrides):
    lookback = 1
    cfg = Config(lookback_days=lookback)
    store = Store(tmp_path / "state.db")
    try:
        stats = ingest(store, cfg, lookback_days=lookback,
                        source_path_overrides=frozen_source_overrides, keep_records=True)
        corr = correlate_records(stats.records)

        persist_correlated(store, stats.records, corr)
        c1 = _counts(store)

        orphan_turns = store.conn.execute(
            "SELECT COUNT(*) FROM turns t "
            "LEFT JOIN sessions s ON t.session_id = s.id WHERE s.id IS NULL"
        ).fetchone()[0]
        orphan_sessions = store.conn.execute(
            "SELECT COUNT(*) FROM sessions s "
            "LEFT JOIN projects p ON s.project_id = p.id WHERE p.id IS NULL"
        ).fetchone()[0]
        empty_fk = store.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE project_id IS NULL OR project_id = ''"
        ).fetchone()[0]

        # Second persist over the SAME correlated records -- must add zero rows.
        persist_correlated(store, stats.records, corr)
        c2 = _counts(store)
    finally:
        store.close()

    for t in ("projects", "sessions", "turns"):
        assert c1[t] > 0, f"canonical table '{t}' is empty"
    assert orphan_turns == 0, f"{orphan_turns} turns reference a missing session"
    assert orphan_sessions == 0, f"{orphan_sessions} sessions reference a missing project"
    assert empty_fk == 0, f"{empty_fk} sessions persisted with empty project_id"
    for t in ("projects", "sessions", "turns"):
        assert c1[t] == c2[t], (
            f"idempotency: {t} changed {c1[t]} -> {c2[t]} on identical re-persist"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
