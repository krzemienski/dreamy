"""OMP and Pi JSONL connectors — same format (Pi is a fork of OMP)."""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..pathdecode import decode_omp_session_dir, prefer_cwd
from ..protocol import Connector, SourceInfo, SourceRecord
from ..redact import redact
from ..store import iso_to_ms

EXCERPT_LEN = 200


class _JsonlConnector(Connector):
    """Shared logic for OMP/Pi — identical JSONL format with type/timestamp/id/parentId/message."""

    # I2: `timestamp` gates the window and `type` selects the branch, in this
    # shared parser both connectors run. Verified against `scan` below.
    FORMAT_KEYS = ("timestamp", "type")

    def _extract_excerpt(self, obj: dict) -> str:
        msg = obj.get("message", {})
        if isinstance(msg, dict):
            content = msg.get("content", "")
        elif isinstance(msg, str):
            content = msg
        else:
            content = ""
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            content = " ".join(parts)
        else:
            content = str(content)
        return redact(content)[:EXCERPT_LEN]

    def discover(self, path_override: str | None = None) -> SourceInfo:
        p = Path(path_override) if path_override else self.default_source_path()
        if not p.exists():
            return SourceInfo(self.SOURCE_ID, False, str(p), error="path not found")
        files = list(p.rglob("*.jsonl")) if p.is_dir() else [p]
        return SourceInfo(self.SOURCE_ID, True, str(p), record_count=len(files))

    def scan(self, watermark_ms: int, lookback_days: int,
             path_override: str | None = None) -> Iterator[SourceRecord]:
        info = self.discover(path_override)
        if not info.available:
            return
        root = Path(info.path)
        files = list(root.rglob("*.jsonl")) if root.is_dir() else [root]

        cutoff = int((datetime.now(UTC) - timedelta(days=lookback_days)).timestamp() * 1000)

        # Decode ONCE per top-level project directory, not once per file.
        # `rglob` yields thousands of files under a handful of project dirs
        # (measured: 4,680 files under 15 dirs), and each decode walks the
        # filesystem. Decoding per file turned a 33s run into a >15min hang.
        #
        # Only the FIRST path segment encodes a project; the rest are OMP's
        # per-day and per-session subdirectories (`<project>/2026-07-24/...`).
        # Passing the whole relative path would hand the decoder an embedded
        # '/', which it correctly rejects as a traversal component.
        decoded: dict[str, str] = {}
        for fpath in files:
            rel_parent = fpath.parent.relative_to(root) if fpath.parent != root else Path(".")
            parts = rel_parent.parts
            key = parts[0] if parts and parts[0] != "." else ""
            if key not in decoded:
                # A malformed encoding raises rather than guessing a path, so
                # it is caught per project: one crafted directory under the
                # harness store must not abort the whole scan. Attribution
                # falls back to the per-record `cwd` for that project alone.
                try:
                    decoded[key] = decode_omp_session_dir(key) if key else ""
                except ValueError as exc:
                    print(
                        f"[WARN] {self.SOURCE_ID}: unusable session directory "
                        f"{key!r}: {exc}",
                        file=sys.stderr,
                    )
                    decoded[key] = ""
            cwd = decoded[key]

            model = ""


            try:
                with fpath.open(errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            print(
                                f"[WARN] {self.SOURCE_ID}: malformed JSON in {fpath.name}, skipping line",
                                file=sys.stderr,
                            )
                            continue
                        if not isinstance(obj, dict):
                            continue

                        ts = iso_to_ms(obj.get("timestamp", ""))
                        if ts < cutoff or ts <= watermark_ms:
                            continue

                        rtype = obj.get("type", "")

                        if rtype == "session":
                            cwd = prefer_cwd(obj.get("cwd"), cwd)
                            model = obj.get("model", "")
                            yield SourceRecord(
                                source_id=self.SOURCE_ID, native_id=obj.get("id", ""),
                                timestamp_ms=ts, record_type="session_start",
                                project_path=cwd, model=model, source_file=str(fpath),
                            )
                        elif rtype == "message":
                            msg = obj.get("message", {})
                            role = msg.get("role", "") if isinstance(msg, dict) else ""
                            yield SourceRecord(
                                source_id=self.SOURCE_ID, native_id=obj.get("id", ""),
                                timestamp_ms=ts, record_type="message",
                                role=role, project_path=cwd, model=model,
                                content_excerpt=self._extract_excerpt(obj),
                                source_file=str(fpath),
                            )
                        elif rtype == "custom":
                            ctype = obj.get("customType", "")
                            data = obj.get("data", {})
                            if isinstance(data, dict) and ctype in ("tool_call", "tool_result", "tool_error"):
                                # An explicit JSON null must not reach the str field.
                                tool_name = data.get("name", ctype) or ctype
                                error_text = redact(str(data.get("error", "")))[:200]
                                yield SourceRecord(
                                    source_id=self.SOURCE_ID, native_id=obj.get("id", ""),
                                    timestamp_ms=ts, record_type="tool_call" if ctype != "tool_error" else "error",
                                    role="tool", project_path=cwd, model=model,
                                    tool_name=tool_name, error_text=error_text,
                                    source_file=str(fpath),
                                )
            except OSError as e:
                print(f"[WARN] {self.SOURCE_ID}: cannot read {fpath}: {e}", file=sys.stderr)


class OmpConnector(_JsonlConnector):
    SOURCE_ID = "omp"
    def default_source_path(self) -> Path:
        return Path.home() / ".omp" / "agent" / "sessions"


class PiConnector(_JsonlConnector):
    SOURCE_ID = "pi"
    def default_source_path(self) -> Path:
        return Path.home() / ".pi" / "agent" / "sessions"
