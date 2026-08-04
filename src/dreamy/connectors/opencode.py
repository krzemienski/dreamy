"""OpenCode SQLite connector — project/session/message/part tables."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..protocol import Connector, SourceInfo, SourceRecord
from ..redact import redact

EXCERPT_LEN = 200


class OpenCodeConnector(Connector):
    SOURCE_ID = "opencode"

    # I2: the exact columns `scan` references per table — SELECT list plus any
    # column used in a WHERE/ORDER BY, since drift in a predicate column
    # breaks the query just as hard as drift in a projected one.
    #
    # Declared once and consumed by the queries below, so the canary asserts
    # what the parser actually reads. A hand-copied second list is a defect,
    # not a convenience: the first draft of the router canary asserted a
    # column that never existed while omitting seven that did.
    #
    # `SELECT_COLUMNS` is the projected subset for tables where the two
    # differ — `message` filters on `session_id` without projecting it.
    TABLE_COLUMNS = {
        "session": (
            "id", "project_id", "directory", "title",
            "time_created", "time_updated", "model", "agent",
        ),
        "message": ("id", "session_id", "time_created", "data"),
        "part": ("message_id", "data", "time_created"),
    }
    SELECT_COLUMNS = {
        "message": ("id", "time_created", "data"),
        "part": ("data",),
    }

    def format_health(self, path_override: str | None = None):
        """I2 — assert the SQLite shape across all three tables `scan` reads.

        Checking only `session` would miss a `message`/`part` change, which is
        where message text actually comes from — drift there yields sessions
        with no content and no warning, the exact silent failure I2 exists for.
        """
        from ..protocol import FormatHealth

        # Not gated on `discover()`: it probes `SELECT COUNT(*) FROM session`,
        # so a database whose `session` table was renamed reports
        # `available=False` and the source reads as MISSING when it is really
        # PRESENT AND DRIFTED. File existence is the honest discriminator.
        db_path = Path(path_override) if path_override else self.default_source_path()
        if not db_path.exists():
            return FormatHealth(self.SOURCE_ID, "absent", f"path not found: {db_path}")
        info = SourceInfo(self.SOURCE_ID, True, str(db_path))
        try:
            c = sqlite3.connect(f"file:{info.path}?mode=ro", uri=True)
            try:
                found = {
                    t: {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
                    for t in self.TABLE_COLUMNS
                }
            finally:
                c.close()
        except sqlite3.Error as exc:
            return FormatHealth(self.SOURCE_ID, "absent", f"cannot read schema: {exc}")
        problems = []
        for table, cols in self.TABLE_COLUMNS.items():
            if not found[table]:
                problems.append(f"table {table} is gone")
                continue
            missing = sorted(set(cols) - found[table])
            if missing:
                problems.append(f"{table} missing {', '.join(missing)}")
        if problems:
            return FormatHealth(self.SOURCE_ID, "drifted", "; ".join(problems), info.path)
        return FormatHealth(self.SOURCE_ID, "ok", "", info.path)

    def default_source_path(self) -> Path:
        return Path.home() / ".local" / "share" / "opencode" / "opencode.db"

    def discover(self, path_override: str | None = None) -> SourceInfo:
        p = Path(path_override) if path_override else self.default_source_path()
        if not p.exists():
            return SourceInfo(self.SOURCE_ID, False, str(p), error="path not found")
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            count = c.execute("SELECT COUNT(*) FROM session").fetchone()[0]
            c.close()
            return SourceInfo(self.SOURCE_ID, True, str(p), record_count=count)
        except sqlite3.Error as e:
            return SourceInfo(self.SOURCE_ID, False, str(p), error=str(e))

    def scan(self, watermark_ms: int, lookback_days: int,
             path_override: str | None = None) -> Iterator[SourceRecord]:
        info = self.discover(path_override)
        if not info.available:
            return
        db_path = Path(info.path)
        cutoff = int((datetime.now(UTC) - timedelta(days=lookback_days)).timestamp() * 1000)
        min_ts = max(cutoff, watermark_ms)

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row

            # Sessions updated within window
            sessions = conn.execute(
                f"SELECT {', '.join(self.TABLE_COLUMNS['session'])} "
                "FROM session WHERE time_updated > ? ORDER BY time_created",
                (min_ts,)
            ).fetchall()

            for srow in sessions:
                project_dir = srow["directory"] or ""
                model = srow["model"] or ""
                ts_created = srow["time_created"]
                # Only emit session_start if the session was CREATED within the window;
                # sessions started earlier but updated recently are tracked via their messages only.
                # Session query remains by time_updated > min_ts to catch long-lived sessions.
                if ts_created > min_ts:
                    yield SourceRecord(
                        source_id=self.SOURCE_ID, native_id=srow["id"],
                        timestamp_ms=ts_created, record_type="session_start",
                        project_path=project_dir, model=model,
                        raw_meta={
                            "title": srow["title"],
                            "agent": srow["agent"],
                            "session_id": srow["id"],
                            "project_id": srow["project_id"],
                        },
                        source_file=str(db_path),
                    )

                # Messages for this session within window
                msgs = conn.execute(
                    f"SELECT {', '.join(self.SELECT_COLUMNS['message'])} FROM message "
                    "WHERE session_id=? AND time_created > ? ORDER BY time_created",
                    (srow["id"], min_ts)
                ).fetchall()

                for mrow in msgs:
                    try:
                        data = json.loads(mrow["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue

                    role = data.get("role", "")
                    parts_text = []

                    # Get parts for this message
                    parts = conn.execute(
                        f"SELECT {', '.join(self.SELECT_COLUMNS['part'])} FROM part "
                        "WHERE message_id=? ORDER BY time_created",
                        (mrow["id"],)
                    ).fetchall()

                    for prow in parts:
                        try:
                            pdata = json.loads(prow["data"])
                        except (json.JSONDecodeError, TypeError):
                            continue
                        ptype = pdata.get("type", "")
                        if ptype == "text":
                            parts_text.append(pdata.get("text", ""))
                        elif ptype == "tool":
                            parts_text.append(f"[tool:{pdata.get('tool', '')}]")

                    yield SourceRecord(
                        source_id=self.SOURCE_ID,
                        native_id=mrow["id"],
                        timestamp_ms=mrow["time_created"],
                        record_type="message",
                        role=role, project_path=project_dir, model=model,
                        content_excerpt=redact(" ".join(parts_text))[:EXCERPT_LEN],
                        source_file=str(db_path),
                        raw_meta={
                            "session_id": srow["id"],
                            "project_id": srow["project_id"],
                            "message_id": mrow["id"],
                        },
                    )

            conn.close()
        except sqlite3.Error as e:
            print(f"[WARN] opencode: database error: {e}", file=sys.stderr)
