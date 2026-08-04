"""Codex JSONL connector — rollout files with payload-wrapped records."""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..protocol import Connector, SourceInfo, SourceRecord
from ..redact import redact
from ..store import iso_to_ms

EXCERPT_LEN = 200


class CodexConnector(Connector):
    SOURCE_ID = "codex"
    # I2: `timestamp` gates the window, `type` selects the branch, `payload`
    # carries every field the branches read. Verified against `scan` below.
    FORMAT_KEYS = ("timestamp", "type", "payload")

    # Codex writes live rollouts to sessions/ and retires them to
    # archived_sessions/. The connector reports the COMMON parent as its source
    # path so a single copy of that directory (snapshot, --source-path, offline
    # bundle) carries both roots; scanning only sessions/ silently drops
    # retired transcripts.
    SUBDIRS = ("sessions", "archived_sessions")

    def default_source_path(self) -> Path:
        return Path.home() / ".codex"

    def _roots(self, path_override: str | None = None) -> list[Path]:
        """Expand a base directory into its rollout roots.

        A path containing any known subdir is treated as a base and expanded.
        Any other path is used verbatim, so a fixture directory of rollout
        files (VG-1b corrupt/outage copies) still scans exactly as given.
        """
        base = Path(path_override) if path_override else self.default_source_path()
        children = [base / d for d in self.SUBDIRS if (base / d).exists()]
        return children or [base]

    def discover(self, path_override: str | None = None) -> SourceInfo:
        base = Path(path_override) if path_override else self.default_source_path()
        roots = [r for r in self._roots(path_override) if r.exists()]
        if not roots:
            return SourceInfo(self.SOURCE_ID, False, str(base), error="path not found")
        files = [f for r in roots for f in r.rglob("*.jsonl")]
        # Report the base so callers that copy `SourceInfo.path` capture every root.
        return SourceInfo(self.SOURCE_ID, True, str(base), record_count=len(files))

    def snapshot_roots(self, path_override: str | None = None) -> list[Path]:
        """Both rollout roots, so a copier captures retired transcripts too."""
        return [r for r in self._roots(path_override) if r.exists()]

    def scan(self, watermark_ms: int, lookback_days: int,
             path_override: str | None = None) -> Iterator[SourceRecord]:
        info = self.discover(path_override)
        if not info.available:
            return
        roots = [r for r in self._roots(path_override) if r.exists()]
        cutoff = int((datetime.now(UTC) - timedelta(days=lookback_days)).timestamp() * 1000)

        for fpath in sorted(f for r in roots for f in r.rglob("*.jsonl")):
            cwd = ""
            malformed = 0
            first_bad_line = 0
            first_bad_reason = ""
            try:
                with fpath.open(errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError as e:
                            malformed += 1
                            if not first_bad_line:
                                first_bad_line = lineno
                                first_bad_reason = str(e)
                            continue
                        if not isinstance(obj, dict):
                            continue

                        ts = iso_to_ms(obj.get("timestamp", ""))
                        if not ts or ts < cutoff or ts <= watermark_ms:
                            continue

                        rtype = obj.get("type", "")
                        payload = obj.get("payload", {})

                        if rtype == "session_meta":
                            cwd = payload.get("cwd", "") or ""
                            yield SourceRecord(
                                source_id=self.SOURCE_ID, native_id=payload.get("id", str(ts)),
                                timestamp_ms=ts, record_type="session_start",
                                project_path=cwd, model=payload.get("model", ""),
                                source_file=str(fpath),
                            )
                        elif rtype == "response_item":
                            item_type = payload.get("type", "")
                            role = payload.get("role", "")
                            content = payload.get("content", [])
                            excerpt = _extract_codex_text(content)
                            if item_type == "message":
                                yield SourceRecord(
                                    source_id=self.SOURCE_ID,
                                    native_id=payload.get("id", str(ts)),
                                    timestamp_ms=ts, record_type="message",
                                    role=role, project_path=cwd,
                                    content_excerpt=excerpt, source_file=str(fpath),
                                )
                            elif item_type == "function_call":
                                yield SourceRecord(
                                    source_id=self.SOURCE_ID,
                                    native_id=payload.get("id", str(ts)),
                                    timestamp_ms=ts, record_type="tool_call",
                                    role="tool", project_path=cwd,
                                    tool_name=payload.get("name", ""),
                                    source_file=str(fpath),
                                )
                        elif rtype == "event_msg":
                            event_type = payload.get("type", "")
                            if "error" in event_type.lower():
                                yield SourceRecord(
                                    source_id=self.SOURCE_ID,
                                    native_id=str(ts),
                                    timestamp_ms=ts, record_type="error",
                                    project_path=cwd,
                                    error_text=redact(str(payload.get("message", "")))[:200],
                                    source_file=str(fpath),
                                )
                if malformed:
                    print(
                        f"[WARN] codex: skipped {malformed} malformed line(s) in {fpath}: "
                        f"line {first_bad_line}: {first_bad_reason}",
                        file=sys.stderr,
                    )
            except OSError as e:
                print(f"[WARN] codex: cannot read {fpath}: {e}", file=sys.stderr)


def _extract_codex_text(content) -> str:
    if isinstance(content, str):
        return redact(content)[:EXCERPT_LEN]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return redact(" ".join(parts))[:EXCERPT_LEN]
    return ""
