"""Claude Code JSONL connector."""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..pathdecode import decode_claude_project_dir, prefer_cwd
from ..protocol import Connector, SourceInfo, SourceRecord
from ..redact import redact
from ..store import iso_to_ms

EXCERPT_LEN = 200


class ClaudeConnector(Connector):
    SOURCE_ID = "claude"
    # I2: the keys `scan` reads off every record before it can classify one.
    # Verified against the parser below — `timestamp` gates the window, `type`
    # selects the branch. If either disappears, scan yields nothing and says
    # nothing, which is exactly the silent-drift failure I2 detects.
    FORMAT_KEYS = ("timestamp", "type")

    def default_source_path(self) -> Path:
        return Path.home() / ".claude" / "projects"

    def discover(self, path_override: str | None = None) -> SourceInfo:
        p = Path(path_override) if path_override else self.default_source_path()
        if not p.exists():
            return SourceInfo(self.SOURCE_ID, False, str(p), error="path not found")
        files = [f for f in p.rglob("*.jsonl") if "subagents" not in f.parts]
        return SourceInfo(self.SOURCE_ID, True, str(p), record_count=len(files))

    def scan(self, watermark_ms: int, lookback_days: int,
             path_override: str | None = None) -> Iterator[SourceRecord]:
        info = self.discover(path_override)
        if not info.available:
            return
        root = Path(info.path)
        cutoff = int((datetime.now(UTC) - timedelta(days=lookback_days)).timestamp() * 1000)

        for fpath in root.rglob("*.jsonl"):
            if "subagents" in fpath.parts:
                continue
            dir_name = fpath.parent.name
            # Directory decode is fallback; per-record cwd is authoritative.
            #
            # A malformed encoding raises rather than guessing a path, so it is
            # caught per file: one crafted directory under the harness store
            # must not abort the whole scan. Attribution degrades to the
            # per-record `cwd` for this file only.
            try:
                dir_cwd = decode_claude_project_dir(dir_name) if dir_name else ""
            except ValueError as exc:
                print(
                    f"[WARN] claude: unusable project directory {dir_name!r}: {exc}",
                    file=sys.stderr,
                )
                dir_cwd = ""


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
                        cwd_val = prefer_cwd(obj.get("cwd"), dir_cwd)
                        git_branch = obj.get("gitBranch", "")

                        if rtype in ("user", "assistant"):
                            msg = obj.get("message", {})
                            raw_role = msg.get("role") if isinstance(msg, dict) else None
                            role = raw_role if isinstance(raw_role, str) else rtype
                            model = msg.get("model", "") if isinstance(msg, dict) else ""
                            content = msg.get("content", "") if isinstance(msg, dict) else ""
                            yield SourceRecord(
                                source_id=self.SOURCE_ID, native_id=obj.get("uuid", ""),
                                timestamp_ms=ts, record_type="message",
                                role=role, project_path=cwd_val, git_branch=git_branch,
                                model=model, content_excerpt=_extract_text(content),
                                source_file=str(fpath),
                            )
                        elif rtype == "system" and obj.get("subtype") == "error":
                            yield SourceRecord(
                                source_id=self.SOURCE_ID, native_id=obj.get("uuid", ""),
                                timestamp_ms=ts, record_type="error",
                                project_path=cwd_val, git_branch=git_branch,
                                error_text=redact(str(obj.get("hookErrors", "")))[:200],
                                source_file=str(fpath),
                            )
                if malformed:
                    print(
                        f"[WARN] claude: skipped {malformed} malformed line(s) in {fpath}: "
                        f"line {first_bad_line}: {first_bad_reason}",
                        file=sys.stderr,
                    )
            except OSError as e:
                print(f"[WARN] claude: cannot read {fpath}: {e}", file=sys.stderr)


def _extract_text(content) -> str:
    if isinstance(content, str):
        return redact(content)[:EXCERPT_LEN]
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool:{block.get('name', '')}]")
        return redact(" ".join(parts))[:EXCERPT_LEN]
    return ""
