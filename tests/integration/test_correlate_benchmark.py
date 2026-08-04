"""Correlate benchmark: index build + matcher vs brute force on real data.

Ingests a small (7-day) lookback window from every live connector source
against a frozen snapshot, then times `correlate_records` over the
resulting records. Real data keeps this meaningful; a synthetic fixture
would not exercise the indexed matcher's actual working set shape.

This snapshots every configured live connector source before ingesting,
which is real disk I/O against real data (some sources hold gigabytes of
history). Opt-in: skipped unless `DREAMY_RUN_ACCEPTANCE=1` is set, in
addition to `@pytest.mark.acceptance`.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from dreamy.config import Config
from dreamy.correlate import correlate_records
from dreamy.ingest import ingest
from dreamy.store import Store

EVIDENCE_DIR = Path.home() / ".local/share/dreamy/acceptance/VG-2-correlate"

pytestmark = pytest.mark.acceptance

skip_reason = (
    "heavy live-source snapshot + ingest; set DREAMY_RUN_ACCEPTANCE=1 to opt in"
)


@pytest.mark.skipif(
    os.environ.get("DREAMY_RUN_ACCEPTANCE") != "1", reason=skip_reason
)
def test_correlate_benchmark_completes_and_produces_evidence(tmp_path: Path, frozen_source_overrides):
    lookback = 7  # small window to keep the benchmark fast
    cfg = Config(lookback_days=lookback)

    db = tmp_path / "state.db"
    store = Store(db)
    try:
        t0 = time.time()
        stats = ingest(store, cfg, lookback_days=lookback,
                        source_path_overrides=frozen_source_overrides, keep_records=True)
        t_ingest = time.time() - t0
    finally:
        store.close()

    t0 = time.time()
    result = correlate_records(stats.records)
    t_correlate = time.time() - t0

    out = {
        "lookback_days": lookback,
        "t_ingest_s": round(t_ingest, 3),
        "t_correlate_s": round(t_correlate, 3),
        "n_records": len(stats.records),
        "n_sessions": len(result.sessions),
        "n_router_links": len(result.router_links),
        "n_unmatched_router": result.unmatched_router,
        "n_projects": len(result.project_paths),
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "benchmark.json").write_text(json.dumps(out, indent=2, default=str))

    assert stats.records, "ingest produced zero records over a 7-day window"
    assert result.sessions, "correlate_records produced zero sessions"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
