"""
Migration ladder self-check.

Scenarios:
  1. Fresh DB  ->  expect schema_version=CURRENT_SCHEMA_VERSION, all required
     columns present, 3 new tables present, PRAGMA integrity_check='ok'.
  2. Synthetic v1-only DB  ->  upgrade applies the ladder in transaction,
     lands at current, all required columns present, PRAGMA
     integrity_check='ok'.
  3. Re-run on already-current DB  ->  no-op idempotent, no errors.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dreamy.store import CURRENT_SCHEMA_VERSION, SCHEMA_V1, Store

REQUIRED_COLUMNS = {
    "findings": ("provenance", "state", "dismissal_reason"),
    "prompt_artifacts": ("chain_json", "health_json", "compiler_version"),
    "runs": ("lookback_days",),
    "sessions": ("platform",),
}
REQUIRED_TABLES = ("agent_calls", "agent_events", "skill_pins")
EXPECTED_COLS = sum(len(v) for v in REQUIRED_COLUMNS.values())


def _assert_schema(db_path: Path) -> None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sv = con.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
        assert sv == CURRENT_SCHEMA_VERSION, (
            f"expected schema_version={CURRENT_SCHEMA_VERSION}, got {sv}"
        )

        versions = [r["version"] for r in con.execute(
            "SELECT version FROM schema_version ORDER BY version").fetchall()]
        assert versions == [CURRENT_SCHEMA_VERSION], (
            f"expected singular schema_version=[{CURRENT_SCHEMA_VERSION}], got {versions}"
        )

        tables = {r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing_tables = [t for t in REQUIRED_TABLES if t not in tables]
        assert not missing_tables, f"missing tables: {missing_tables}"

        found_cols = 0
        missing_cols = []
        for tbl, cols in REQUIRED_COLUMNS.items():
            assert tbl in tables, f"missing table {tbl}"
            existing = {r["name"] for r in con.execute(f"PRAGMA table_info({tbl})")}
            for c in cols:
                if c in existing:
                    found_cols += 1
                else:
                    missing_cols.append(f"{tbl}.{c}")
        assert not missing_cols, f"missing columns: {missing_cols}"
        assert found_cols == EXPECTED_COLS

        ic = con.execute("PRAGMA integrity_check").fetchone()[0]
        assert ic == "ok", f"integrity_check returned {ic!r}, expected 'ok'"
    finally:
        con.close()


def test_fresh_db_lands_at_current_schema(tmp_path: Path) -> None:
    p = tmp_path / "fresh.db"
    s = Store(p)
    s.close()
    _assert_schema(p)


def test_v1_db_upgrades_to_current_schema(tmp_path: Path) -> None:
    p = tmp_path / "v1.db"
    con = sqlite3.connect(str(p))
    con.executescript(SCHEMA_V1)
    con.commit()
    con.close()
    # Opening with Store detects v1 and applies the migration ladder.
    s = Store(p)
    s.close()
    _assert_schema(p)


def test_rerun_on_current_db_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "v2.db"
    s = Store(p)  # first run creates the schema
    s.close()
    # Opening again must be a clean no-op.
    s2 = Store(p)
    s2.close()
    _assert_schema(p)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
