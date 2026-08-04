"""Shared fixtures for tests/integration acceptance checks.

Several integration tests snapshot every live connector source into a
temporary directory before running the production ingest/correlate/persist
pipeline against the frozen copy, so concurrent live writes cannot fake or
break an idempotency result. The `frozen_source_overrides` fixture is that
shared, socket/fifo-safe snapshot routine, exposed via pytest's fixture
injection so consuming test modules need no cross-module import.
"""
from __future__ import annotations

import shutil
import sqlite3
import stat
from pathlib import Path

import pytest


def _safe_copy_tree(src: Path, dest: Path, errors: list[str]) -> None:
    """Recursive copy that skips sockets, fifos, and other non-regular
    special files a live connector source may contain (e.g. a running
    agent's `ipc.sock`). `shutil.copytree` raises on these; a snapshot for
    evidence/read-only replay never needs them, so they are skipped rather
    than aborting the whole snapshot.

    Regular files that fail to copy are NOT silently skipped -- a frozen
    snapshot underpins an idempotency assertion, so a missing regular file
    would silently produce a "clean" second run that never actually saw
    the same input. Failures are appended to `errors` and raised by the
    caller once the whole tree has been walked."""
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
        # sockets, fifos, symlinks, device files: silently skipped -- a
        # snapshot never needs to reproduce IPC endpoints or link targets.


def _snapshot_live_sources(snap_root: Path) -> dict[str, str]:
    """Snapshot every live connector source into snap_root.

    Returns a mapping of SOURCE_ID -> frozen path usable as a
    `source_path_overrides` value. SQLite sources are copied via SQLite's
    online backup API for a consistent snapshot. Directory sources are
    recursively copied via `_safe_copy_tree`, using
    `Connector.snapshot_roots()` so multi-root sources (e.g. Codex
    `sessions/` + `archived_sessions/`) are captured completely.
    """
    from dreamy.connectors import make_connectors

    overrides: dict[str, str] = {}
    errors: list[str] = []
    snap_root.mkdir(parents=True, exist_ok=True)
    for c in make_connectors():
        info = c.discover()
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
    return overrides


@pytest.fixture
def frozen_source_overrides(tmp_path: Path) -> dict[str, str]:
    """Frozen `source_path_overrides` snapshot of every available live
    connector source, taken once at fixture setup. Callers that run the
    real ingest/correlate/persist pipeline twice against these overrides
    see identical input on both runs, making idempotency assertions
    meaningful."""
    return _snapshot_live_sources(tmp_path / "source-snapshot")
