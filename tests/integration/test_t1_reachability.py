"""T1 reachability selfcheck -- is tier-1 correlation possible on this machine?

Tier 1 links a router request to a session by **exact identifier overlap**
(`correlate.py:_match_router_indexed`, tier 1 branch). It fires only when a
value the router records also appears in the session-side id_index, which is
populated from a session's `native_id`, its member turn `native_id`s, and
id-shaped values in turn `raw_meta`.

`DESIGN.md` §3.3 hedges T1 explicitly: *"Only when a source records the
other's ID."* This test measures whether any source on this machine
actually does.

It exists because amending a gate criterion is the most abuse-prone action
available, and a one-off query is not a sufficient evidence bar for it. A
third party must be able to re-derive the finding, and must be able to
detect when it stops being true.

Read-only. Opens the DB with `mode=ro` and never writes to it.

Real-system acceptance check: requires a populated `state.db`. Skipped
when that database is absent (e.g. a fresh checkout with no ingested
data yet).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

DEFAULT_DB = Path("~/.local/share/dreamy/state.db").expanduser()

# Mirrors `_id_shaped_values`: only values long enough to be a real
# identifier are considered, so short numeric fields cannot produce
# accidental overlap.
_MIN_ID_LEN = 8

pytestmark = pytest.mark.acceptance

skip_reason = f"no populated state.db at {DEFAULT_DB}; T1 reachability is unmeasurable without live data"


def _id_shaped(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= _MIN_ID_LEN


def _raw_meta_ids(blob: str | None) -> list[str]:
    """Id-shaped values inside a turn's raw_meta_json, one level deep.

    Matches what `_extract_router_id_candidates` reaches via
    `_id_shaped_values`.
    """
    if not blob:
        return []
    try:
        obj = json.loads(blob)
    except (ValueError, TypeError):
        return []
    out: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            if _id_shaped(v):
                out.append(v)
            elif isinstance(v, dict):
                out.extend(x for x in v.values() if _id_shaped(x))
    return out


def measure(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        # --- session-side key space: what id_index would hold ---
        session_ids = {r[0] for r in con.execute(
            "SELECT native_id FROM sessions WHERE native_id IS NOT NULL AND native_id != ''")}
        turn_ids = {r[0] for r in con.execute(
            "SELECT id FROM turns WHERE id IS NOT NULL AND id != ''")}
        raw_ids: set[str] = set()
        for (blob,) in con.execute(
                "SELECT raw_meta_json FROM turns WHERE raw_meta_json IS NOT NULL"):
            raw_ids.update(_raw_meta_ids(blob))
        left = session_ids | turn_ids | raw_ids

        # --- router-side key space: what _extract_router_id_candidates yields ---
        router_native = {str(r[0]) for r in con.execute(
            "SELECT native_id FROM router_requests WHERE native_id IS NOT NULL AND native_id != ''")}
        router_conn = {r[0] for r in con.execute(
            "SELECT DISTINCT connection_id FROM router_requests "
            "WHERE connection_id IS NOT NULL AND connection_id != ''")}
        right = router_native | router_conn

        overlap = sorted(left & right)

        return {
            "db": str(db_path),
            "left_key_space": {
                "session_native_ids": len(session_ids),
                "turn_ids": len(turn_ids),
                "raw_meta_ids": len(raw_ids),
                "total_distinct": len(left),
            },
            "right_key_space": {
                "router_native_ids": len(router_native),
                "distinct_connection_ids": len(router_conn),
                "total_distinct": len(right),
            },
            "overlap_count": len(overlap),
            "overlap_sample": overlap[:10],
            "t1_reachable": bool(overlap),
            "verdict": "T1_REACHABLE" if overlap else "T1_UNREACHABLE",
        }
    finally:
        con.close()


@pytest.mark.skipif(not DEFAULT_DB.is_file(), reason=skip_reason)
def test_t1_reachability_matches_design_hedge():
    """DESIGN.md §3.3 asserts T1 is unreachable in practice on this fleet.

    If this ever flips to reachable, the amendment the design doc rests
    on is invalid and must be re-derived -- this test is the trip wire.
    """
    result = measure(DEFAULT_DB)
    assert result["verdict"] == "T1_UNREACHABLE", (
        "T1 IS REACHABLE -- any amendment premised on unreachability is now "
        f"INVALID. Overlap sample: {result['overlap_sample']}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
