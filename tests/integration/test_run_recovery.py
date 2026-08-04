"""Orphan-run recovery (P0).

A run row is opened `running` and only updated at completion, so a process
killed mid-run leaves the row `running` forever. Nothing reconciled them:
measured on a real machine, three orphans accumulated and two later successful
runs walked straight past them, which meant `status='running'` could never be
trusted to mean "a run is happening now".

These tests pin the three properties that make recovery safe:
  1. it closes abandoned rows,
  2. it never touches rows that already ended, and
  3. it is ordered so it cannot close the caller's own run.

Property 3 is the one worth guarding hardest — `recover_interrupted_runs` uses
a blanket `WHERE status='running'` predicate, which is only correct because the
caller holds the exclusive RunLock AND has not yet inserted its own row.
Reversing that order would make every run mark itself interrupted.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dreamy.store import Store


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def _statuses(s: Store) -> dict[str, str]:
    return {r["id"]: r["status"] for r in s.conn.execute("SELECT id, status FROM runs")}


def test_recovers_abandoned_run(store: Store) -> None:
    """The core case: a row left `running` by a killed process is closed."""
    store.start_run("run-dead", 1_000, lookback_days=30)
    store.commit()
    assert _statuses(store)["run-dead"] == "running"

    recovered = store.recover_interrupted_runs(9_000)
    store.commit()

    assert recovered == 1
    assert _statuses(store)["run-dead"] == "interrupted"


def test_recovery_preserves_start_and_metadata(store: Store) -> None:
    """Recovery closes a row; it must not rewrite what the run reported.

    `ended_ms` is stamped with the recovery time rather than back-dated to the
    start: the run really did occupy the machine until something ended it, and
    a zero-length run would be a fiction.
    """
    store.start_run("run-dead", 1_000, lookback_days=7)
    store.commit()

    store.recover_interrupted_runs(9_000)
    store.commit()

    row = store.conn.execute(
        "SELECT started_ms, ended_ms, lookback_days FROM runs WHERE id='run-dead'"
    ).fetchone()
    assert row["started_ms"] == 1_000, "original start must survive"
    assert row["lookback_days"] == 7, "reported window must survive"
    assert row["ended_ms"] == 9_000, "ended_ms is the recovery stamp"


def test_completed_runs_are_untouched(store: Store) -> None:
    """A finished run is not an orphan and must never be reclassified."""
    store.start_run("run-ok", 1_000)
    store.conn.execute(
        "UPDATE runs SET status='ok', ended_ms=2000 WHERE id='run-ok'"
    )
    store.commit()

    recovered = store.recover_interrupted_runs(9_000)
    store.commit()

    assert recovered == 0
    row = store.conn.execute(
        "SELECT status, ended_ms FROM runs WHERE id='run-ok'"
    ).fetchone()
    assert row["status"] == "ok"
    assert row["ended_ms"] == 2000, "a completed run's end time must not move"


def test_recovery_is_idempotent(store: Store) -> None:
    """Running twice recovers nothing the second time.

    Guards against a predicate that re-selects already-interrupted rows and
    keeps advancing their `ended_ms` on every subsequent run.
    """
    store.start_run("run-dead", 1_000)
    store.commit()

    assert store.recover_interrupted_runs(9_000) == 1
    store.commit()
    assert store.recover_interrupted_runs(20_000) == 0
    store.commit()

    row = store.conn.execute(
        "SELECT ended_ms FROM runs WHERE id='run-dead'"
    ).fetchone()
    assert row["ended_ms"] == 9_000, "a second pass must not re-stamp the row"


def test_recovers_multiple_orphans(store: Store) -> None:
    """Three orphans accumulated on the real machine before this existed."""
    for i in range(3):
        store.start_run(f"run-dead-{i}", 1_000 + i)
    store.commit()

    assert store.recover_interrupted_runs(9_000) == 3
    store.commit()
    assert set(_statuses(store).values()) == {"interrupted"}


def test_ordering_contract_current_run_must_not_self_recover(store: Store) -> None:
    """The ordering bug this API is easiest to misuse into.

    `recover_interrupted_runs` matches every `running` row. Called AFTER
    `start_run`, it closes the caller's own freshly-opened run — the pipeline
    would mark itself interrupted on every single invocation. This test pins
    both orders so the correct one cannot be silently swapped.
    """
    # Correct order: recover first, THEN open our row.
    store.start_run("run-old-orphan", 1_000)
    store.commit()
    recovered = store.recover_interrupted_runs(5_000)
    store.start_run("run-current", 5_000)
    store.commit()

    statuses = _statuses(store)
    assert recovered == 1
    assert statuses["run-old-orphan"] == "interrupted"
    assert statuses["run-current"] == "running", (
        "the run that just started must still be running"
    )

    # Wrong order, asserted explicitly: recovering after start_run would close
    # the current run too. Documented here so the hazard is visible in the
    # suite rather than only in a comment.
    store.recover_interrupted_runs(6_000)
    store.commit()
    assert _statuses(store)["run-current"] == "interrupted", (
        "demonstrates why recovery MUST precede start_run"
    )




def test_duration_excludes_interrupted_runs(store: Store) -> None:
    """N1 timing must not absorb recovery latency.

    An interrupted run's `ended_ms` is the RECOVERY stamp, not the moment the
    process died — nothing records that. Measured on the real machine: a run
    killed ~1s in reported `dur=16.1s` because a later run recovered it 16s
    afterwards. Surfacing that as a duration would inflate the N1
    incremental-run budget with time no run spent working.
    """
    from dreamy.read import ReadStore, _duration_ms

    store.start_run("run-killed", 1_000)
    store.commit()
    store.recover_interrupted_runs(17_100)
    store.commit()

    row = store.conn.execute(
        "SELECT started_ms, ended_ms, status FROM runs WHERE id='run-killed'"
    ).fetchone()
    assert row["ended_ms"] - row["started_ms"] == 16_100, "fixture premise"
    assert _duration_ms(row) is None, "interrupted runs report no duration"

    # And the surface agrees — the helper is not bypassed by a stray inline
    # subtraction somewhere in ReadStore.
    rs = ReadStore(store.db_path, read_only=True)
    try:
        summaries = {r.id: r for r in rs.runs_history(limit=5)}
    finally:
        rs.close()
    assert summaries["run-killed"].duration_ms is None
    assert summaries["run-killed"].status == "interrupted"


def test_duration_reported_for_completed_runs(store: Store) -> None:
    """The exclusion is narrow: a normal run still reports its duration."""
    from dreamy.read import _duration_ms

    store.start_run("run-ok", 1_000)
    store.conn.execute("UPDATE runs SET status='ok', ended_ms=12000 WHERE id='run-ok'")
    store.commit()

    row = store.conn.execute(
        "SELECT started_ms, ended_ms, status FROM runs WHERE id='run-ok'"
    ).fetchone()
    assert _duration_ms(row) == 11_000