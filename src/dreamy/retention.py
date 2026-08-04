"""Retention purger (Q8). 90-day default; never touches state.db, projects/, config.json."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PurgeStats:
    runs_removed: int = 0
    archive_removed: int = 0
    logs_removed: int = 0


def _is_older(p: Path, cutoff_ms: int) -> bool:
    try:
        return p.stat().st_mtime * 1000 < cutoff_ms
    except OSError:
        return False


def _purge_dir(dirpath: Path, cutoff_ms: int) -> int:
    if not dirpath.is_dir():
        return 0
    removed = 0
    for child in dirpath.iterdir():
        if child.is_file() and _is_older(child, cutoff_ms):
            try:
                child.unlink()
                removed += 1
            except OSError:
                pass
        elif child.is_dir() and _is_older(child, cutoff_ms):
            try:
                # Tombstone for visibility.
                (child / ".retention-removed").write_text(
                    f"removed at {int(time.time() * 1000)}\n", encoding="utf-8"
                )
                for inner in list(child.iterdir()):
                    if inner.is_file():
                        try:
                            inner.unlink()
                        except OSError:
                            pass
                removed += 1
            except OSError:
                pass
    return removed


def purge_old_artifacts(output_dir: Path, retention_days: int) -> PurgeStats:
    output_dir = Path(output_dir)
    cutoff_ms = int(time.time() * 1000) - retention_days * 24 * 3600 * 1000
    stats = PurgeStats()
    stats.runs_removed = _purge_dir(output_dir / "runs", cutoff_ms)
    stats.archive_removed = _purge_dir(output_dir / "reports" / "archive", cutoff_ms)
    stats.logs_removed = _purge_dir(output_dir / "logs", cutoff_ms)
    return stats
