"""VG-1 evidence capture -- ingest + correlate + persist via the PRODUCTION path.

This test implements no persistence of its own. It calls
``run.run_pipeline``, the same entrypoint a user or launchd invokes, so
the evidence proves the shipped code path rather than a parallel
acceptance-only implementation.

Captures per-source record counts, canonical table counts, referential
integrity, and an idempotency check (a second run over a frozen source
snapshot must add zero rows).

Snapshots every configured live connector source, some of which hold
gigabytes of session history. This is real disk I/O against real data, so
this test is opt-in: it is skipped unless `DREAMY_RUN_ACCEPTANCE=1` is set
in the environment, in addition to `@pytest.mark.acceptance`.

Evidence -> ~/.local/share/dreamy/acceptance/VG-1[/<suffix>]/
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

import pytest

from dreamy import run as run_mod
from dreamy.config import Config
from dreamy.connectors import make_connectors
from dreamy.store import CURRENT_SCHEMA_VERSION

CANONICAL_TABLES = ("projects", "sessions", "turns", "router_requests")

pytestmark = pytest.mark.acceptance

skip_reason = (
    "heavy live-source snapshot + double run_pipeline; "
    "set DREAMY_RUN_ACCEPTANCE=1 to opt in"
)


def _safe_copy_tree(src: Path, dest: Path, errors: list[str]) -> None:
    """Recursive copy that skips sockets, fifos, and other non-regular
    special files a live connector source may contain (e.g. a running
    agent's `ipc.sock`); regular-file copy failures are recorded rather
    than silently dropped, since a frozen snapshot underpins the
    idempotency comparison this test relies on."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dest / entry.name
        try:
            st = entry.lstat()
        except OSError as e:
            errors.append(f"{entry}: lstat failed: {e}")
            continue
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            _safe_copy_tree(entry, target, errors)
        elif stat.S_ISREG(st.st_mode):
            try:
                shutil.copy2(entry, target)
            except OSError as e:
                errors.append(f"{entry}: copy failed: {e}")
        # sockets, fifos, symlinks, device files: silently skipped.


def _snapshot_sources(snap_root: Path):
    """Freeze every available source so live writes cannot affect idempotency."""
    overrides = {}
    errors: list[str] = []
    discovered = []
    snap_root.mkdir(parents=True, exist_ok=True)
    for c in make_connectors():
        info = c.discover()
        discovered.append({
            "source_id": c.SOURCE_ID,
            "available": bool(info.available),
            "path": info.path or "",
        })
        if not (info.available and info.path):
            continue
        src = Path(info.path)
        if not src.exists():
            continue
        dest = snap_root / c.SOURCE_ID
        if src.is_dir():
            roots = c.snapshot_roots() or [src]
            for root in roots:
                if not root.exists():
                    continue
                try:
                    rel = root.relative_to(src)
                except ValueError:
                    rel = Path(root.name)
                target = dest if str(rel) == "." else dest / rel
                _safe_copy_tree(root, target, errors)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            dst_con = sqlite3.connect(str(dest))
            try:
                with dst_con:
                    src_con.backup(dst_con)
            finally:
                src_con.close()
                dst_con.close()
        overrides[c.SOURCE_ID] = str(dest)
    if errors:
        raise OSError(
            f"{len(errors)} regular file(s) failed to snapshot; the frozen "
            f"copy is incomplete and cannot back an idempotency assertion:\n"
            + "\n".join(errors[:20])
        )
    return overrides, discovered


def _table_counts(db_path: Path) -> dict:
    if not db_path.exists():
        return {t: 0 for t in CANONICAL_TABLES}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = {}
    try:
        for t in CANONICAL_TABLES:
            try:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = None
    finally:
        con.close()
    return out


def _integrity(db_path: Path) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            "orphan_turns": con.execute(
                "SELECT COUNT(*) FROM turns t LEFT JOIN sessions s "
                "ON t.session_id = s.id WHERE s.id IS NULL").fetchone()[0],
            "orphan_sessions": con.execute(
                "SELECT COUNT(*) FROM sessions s LEFT JOIN projects p "
                "ON s.project_id = p.id WHERE p.id IS NULL").fetchone()[0],
            "empty_project_fk": con.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id IS NULL "
                "OR project_id = ''").fetchone()[0],
            "schema_version": [r[0] for r in con.execute(
                "SELECT version FROM schema_version").fetchall()],
            "integrity_check": con.execute(
                "PRAGMA integrity_check").fetchone()[0],
        }
    finally:
        con.close()


def _schema_text(db_path: Path) -> str:
    if not db_path.exists():
        return "(no db)"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()
        return "\n".join(r[0] + ";" for r in rows)
    finally:
        con.close()


def run_once(lookback_days: int, evidence_dir: Path) -> int:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    snap = Path(tempfile.mkdtemp(prefix="dreamy-vg1-"))
    out_dir = Path(tempfile.mkdtemp(prefix="dreamy-vg1-out-"))
    failures = []
    try:
        overrides, discovered = _snapshot_sources(snap)
        cfg = Config(lookback_days=lookback_days)

        t0 = time.time()
        r1 = run_mod.run_pipeline(cfg, output_dir=out_dir,
                                   lookback_days=lookback_days,
                                   source_path_overrides=overrides)
        d1 = time.time() - t0
        db = out_dir / "state.db"
        c1 = _table_counts(db)
        integ = _integrity(db)

        run1 = {
            "lookback_days": lookback_days,
            "status": r1.status,
            "duration_s": round(d1, 2),
            "sources": [
                {**d, "records": r1.source_counts.get(d["source_id"], 0)}
                for d in discovered
            ],
            "table_counts": c1,
            "integrity": integ,
            "warning_count": r1.warning_count,
        }
        (evidence_dir / "run1.json").write_text(json.dumps(run1, indent=2))

        # Second run over the SAME frozen snapshot -- must add zero rows.
        t0 = time.time()
        r2 = run_mod.run_pipeline(cfg, output_dir=out_dir,
                                   lookback_days=lookback_days,
                                   source_path_overrides=overrides)
        d2 = time.time() - t0
        c2 = _table_counts(db)
        (evidence_dir / "run2.json").write_text(json.dumps({
            "status": r2.status,
            "duration_s": round(d2, 2),
            "table_counts": c2,
        }, indent=2))

        (evidence_dir / "schema.txt").write_text(_schema_text(db))

        # ---- pass criteria ----
        # A present source yielding zero records is only acceptable when the
        # source genuinely holds nothing inside the lookback window. Blanket
        # -allowing zero would hide a broken scanner (the exact failure this
        # gate exists to catch); blanket-failing it wrongly condemns a harness
        # the operator simply has not used lately — measured here: codex's
        # newest session predates a 30-day window by weeks.
        #
        # The discriminator is evidence, not policy: re-scan that source with
        # NO watermark and an unbounded window. Records outside the window but
        # none inside means `empty_window`; nothing at all from a source that
        # reports available means the scanner is broken.
        cutoff_ms = int((time.time() - lookback_days * 86400) * 1000)
        present_zero = []
        for d in run1["sources"]:
            if not (d["available"] and d["records"] == 0):
                continue
            source_id = d["source_id"]
            newest = 0
            total = 0
            try:
                connector = next(
                    c for c in make_connectors() if c.SOURCE_ID == source_id
                )
                # NO cap. An early-exit would make `newest` the newest of the
                # first N records scanned, not the newest overall — and scan
                # order is filesystem order, never guaranteed chronological.
                # A capped maximum cannot prove "nothing newer exists", which
                # is the entire claim this branch rests on.
                for rec in connector.scan(
                    watermark_ms=0, lookback_days=36500,
                    path_override=overrides.get(source_id),
                ):
                    total += 1
                    newest = max(newest, rec.timestamp_ms or 0)
            except Exception as exc:  # noqa: BLE001 - classified, not raised
                failures.append(f"{source_id}: zero records AND rescan failed: {exc!r}")
                continue
            if total == 0:
                failures.append(
                    f"{source_id}: reports available but yields no records at any depth"
                )
            elif newest >= cutoff_ms:
                failures.append(
                    f"{source_id}: has records newer than the cutoff but scanned zero "
                    f"(newest={newest}, cutoff={cutoff_ms}) — scanner defect"
                )
            else:
                present_zero.append(
                    {"source_id": source_id, "verdict": "empty_window",
                     "newest_ms": newest, "cutoff_ms": cutoff_ms, "records_all_time": total}
                )
        (evidence_dir / "empty-window-sources.json").write_text(
            json.dumps(present_zero, indent=2)
        )
        for t in ("projects", "sessions", "turns"):
            if not c1.get(t):
                failures.append(f"canonical table '{t}' empty after run1")
        diffs = {t: [c1.get(t), c2.get(t)] for t in CANONICAL_TABLES
                 if c1.get(t) != c2.get(t)}
        if diffs:
            failures.append(f"idempotency: table counts changed {diffs}")
        if integ["orphan_turns"] or integ["orphan_sessions"] or integ["empty_project_fk"]:
            failures.append(f"referential integrity: {integ}")
        if integ["schema_version"] != [CURRENT_SCHEMA_VERSION]:
            failures.append(f"schema_version != [{CURRENT_SCHEMA_VERSION}]: {integ['schema_version']}")
        if integ["integrity_check"] != "ok":
            failures.append(f"PRAGMA integrity_check: {integ['integrity_check']}")
        if r1.status != "ok":
            failures.append(f"run1 status={r1.status}")

        (evidence_dir / "verdict.json").write_text(json.dumps({
            "gate": "VG-1",
            "lookback_days": lookback_days,
            "verdict": "FAIL" if failures else "PASS",
            "failures": failures,
            "run1": run1,
            "run2_table_counts": c2,
        }, indent=2))

        print(f"=== VG-1 @ {lookback_days}d (production run_pipeline) ===")
        for d in run1["sources"]:
            mark = ""
            if d["available"] and d["records"] == 0:
                mark = "   <-- ZERO"
            print(f"  {d['source_id']:12s} available={str(d['available']):5s} "
                  f"records={d['records']}{mark}")
        print(f"tables run1: {c1}")
        print(f"tables run2: {c2}")
        print(f"integrity:   {integ}")
        print(f"duration:    run1={d1:.1f}s run2={d2:.1f}s")
        if failures:
            print("\nFAILURES:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("\nPASS")
        return 0
    finally:
        shutil.rmtree(snap, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


@pytest.mark.skipif(
    os.environ.get("DREAMY_RUN_ACCEPTANCE") != "1", reason=skip_reason
)
def test_vg1_correlate_capture_gate_passes():
    base = Path.home() / ".local/share/dreamy/acceptance/VG-1"
    exit_code = run_once(30, base)
    assert exit_code == 0, (
        f"VG-1 correlate-capture gate failed; see {base / 'verdict.json'} for details"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
