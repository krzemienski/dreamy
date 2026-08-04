"""Nine Router SQLite connector — usageHistory table (authoritative request log)."""
from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..protocol import Connector, SourceInfo, SourceRecord

# Nine Router stores timestamps as ISO text strings.
# usageHistory columns: id, timestamp, provider, model, connectionId, apiKey,
#   endpoint, promptTokens, completionTokens, cost, status, tokens, meta


class RouterConnector(Connector):
    SOURCE_ID = "nine_router"

    # I2: the exact column set `scan` selects. Declared ONCE and consumed by
    # both the query and the canary, because a hand-copied list is a second
    # copy of the truth that drifts from the parser it is meant to protect —
    # the first draft of this canary asserted a `createdAt` column that has
    # never existed and omitted seven that do, so it would have reported a
    # healthy database as drifted while missing real drift.
    USAGE_COLUMNS = (
        "id", "timestamp", "provider", "model", "connectionId", "endpoint",
        "promptTokens", "completionTokens", "cost", "status",
    )

    def format_health(self, path_override: str | None = None):
        """I2 — assert the SQLite shape, not a JSONL one.

        The base implementation samples `*.jsonl`; this source is a SQLite
        database, so inheriting it would report `absent` forever and quietly
        exempt the connector from drift detection.
        """
        from ..protocol import FormatHealth

        # Deliberately NOT gated on `discover()`. `discover` probes with
        # `SELECT COUNT(*) FROM usageHistory`, so a database whose table was
        # renamed or dropped reports `available=False` — the source looks
        # MISSING when it is actually PRESENT AND DRIFTED. Returning `absent`
        # there would silently exempt the worst drift case from detection,
        # which is the exact failure I2 exists to catch. The file's existence
        # is what separates the two, so that is what is checked.
        db_path = Path(path_override) if path_override else self.default_source_path()
        if not db_path.exists():
            return FormatHealth(self.SOURCE_ID, "absent", f"path not found: {db_path}")
        info = SourceInfo(self.SOURCE_ID, True, str(db_path))
        try:
            c = sqlite3.connect(f"file:{info.path}?mode=ro", uri=True)
            try:
                cols = {r[1] for r in c.execute("PRAGMA table_info(usageHistory)")}
            finally:
                c.close()
        except sqlite3.Error as exc:
            return FormatHealth(self.SOURCE_ID, "absent", f"cannot read schema: {exc}")
        if not cols:
            return FormatHealth(self.SOURCE_ID, "drifted", "table usageHistory is gone", info.path)
        missing = sorted(set(self.USAGE_COLUMNS) - cols)
        if missing:
            return FormatHealth(
                self.SOURCE_ID, "drifted",
                f"usageHistory missing column(s) {', '.join(missing)}", info.path,
            )
        return FormatHealth(self.SOURCE_ID, "ok", "", info.path)

    def default_source_path(self) -> Path:
        return Path.home() / ".9router" / "db" / "data.sqlite"

    def discover(self, path_override: str | None = None) -> SourceInfo:
        p = Path(path_override) if path_override else self.default_source_path()
        if not p.exists():
            return SourceInfo(self.SOURCE_ID, False, str(p), error="path not found")
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            count = c.execute("SELECT COUNT(*) FROM usageHistory").fetchone()[0]
            c.close()
            return SourceInfo(self.SOURCE_ID, True, str(p), record_count=count)
        except sqlite3.Error as e:
            return SourceInfo(self.SOURCE_ID, False, str(p), error=str(e))

    def scan(self, watermark_ms: int, lookback_days: int,
             path_override: str | None = None) -> Iterator[SourceRecord]:
        from ..store import iso_to_ms
        info = self.discover(path_override)
        if not info.available:
            return
        db_path = Path(info.path)
        cutoff_dt = datetime.now(UTC) - timedelta(days=lookback_days)
        cutoff_str = cutoff_dt.strftime('%Y-%m-%dT%H:%M:%S') + f'.{cutoff_dt.microsecond // 1000:03d}Z'

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row

            # SQL filters by lookback cutoff using lexicographic ISO comparison.
            # Watermark and exact cutoff boundary enforced NUMERICALLY in Python below,
            # because stored ISO format (Z suffix) differs from Python isoformat() (+00:00),
            # causing lexicographic boundary mismatches.
            # Semantics: ALL records with ts_ms <= watermark_ms are excluded (exclusive boundary).
            cutoff_ms = int(cutoff_dt.timestamp() * 1000)
            rows = conn.execute(
                f"SELECT {', '.join(self.USAGE_COLUMNS)} "
                "FROM usageHistory WHERE timestamp > ? ORDER BY timestamp",
                (cutoff_str,)
            ).fetchall()

            for row in rows:
                ts_ms = iso_to_ms(row["timestamp"])
                if not ts_ms or ts_ms < cutoff_ms or ts_ms <= watermark_ms:
                    continue
                yield SourceRecord(
                    source_id=self.SOURCE_ID,
                    native_id=str(row["id"]),
                    timestamp_ms=ts_ms,
                    record_type="router_request",
                    model=row["model"] or "",
                    raw_meta={
                        "provider": row["provider"],
                        "connection_id": row["connectionId"],
                        "endpoint": row["endpoint"],
                        "prompt_tokens": row["promptTokens"],
                        "completion_tokens": row["completionTokens"],
                        "cost_usd": row["cost"],
                        "status": row["status"],
                    },
                    source_file=str(db_path),
                )

            conn.close()
        except sqlite3.Error as e:
            print(f"[WARN] nine_router: database error: {e}", file=sys.stderr)
