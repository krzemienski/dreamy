"""Connector protocol — all six source connectors implement this ABC."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SourceInfo:
    """Metadata about a discovered source."""
    source_id: str
    available: bool
    path: str
    record_count: int = 0
    error: str | None = None


@dataclass
class FormatHealth:
    """I2 — whether a source's on-disk shape still matches what we parse.

    `outcome` is one of:
      ok       — a real file/table was sampled and carried what we read
      drifted  — a real file/table was sampled and did NOT carry it
      absent   — nothing to sample; the harness is not installed here
      error    — the check itself failed; set by the caller, never returned
                 by a connector. Distinct from `absent` because `absent` is a
                 normal state rendered quietly, and a broken guard filed under
                 it would stop guarding without anyone noticing.

    `drifted` is deliberately NOT an error. A silent shape change is the
    failure this detects, so turning detection into a crash would trade a
    quiet wrong answer for a loud missing one. Five healthy sources still
    ingest.
    """
    source_id: str
    outcome: str
    detail: str = ""
    sampled_file: str = ""


@dataclass
class SourceRecord:
    """Normalized record from any source, before canonical entity mapping."""
    source_id: str
    native_id: str
    timestamp_ms: int
    record_type: str           # session_start, message, tool_call, file_edit, error, session_end
    role: str = ""             # user, assistant, system, tool
    project_path: str = ""
    git_branch: str = ""
    model: str = ""
    content_excerpt: str = ""  # redacted, truncated for fingerprinting
    tool_name: str = ""
    file_paths: list[str] = field(default_factory=list)
    error_text: str = ""
    raw_meta: dict[str, Any] = field(default_factory=dict)
    source_file: str = ""


class Connector(ABC):
    """Protocol every connector implements. path_override enables synthetic-fault testing."""

    SOURCE_ID: str = ""

    @abstractmethod
    def default_source_path(self) -> Path:
        """Return the default on-disk path for this source."""

    @abstractmethod
    def discover(self, path_override: str | None = None) -> SourceInfo:
        """Check if the source exists and report basic metadata."""

    @abstractmethod
    def scan(self, watermark_ms: int, lookback_days: int,
             path_override: str | None = None) -> Iterator[SourceRecord]:
        """Yield normalized records newer than watermark_ms within lookback_days.

        On missing source: yield nothing, let discover() report the gap.
        On corrupt record: skip it, emit a warning via stderr, continue.
        """

    def snapshot_roots(self, path_override: str | None = None) -> list[Path]:
        """Return every path a copier must capture to reproduce this source.

        Defaults to the single discovered path. Connectors whose data spans
        multiple directories (e.g. Codex ``sessions/`` + ``archived_sessions/``)
        override this so snapshots, ``--source-path`` copies, and offline
        bundles never silently drop a root.
        """
        info = self.discover(path_override)
        return [Path(info.path)] if info.available and info.path else []

    # I2: the keys a connector actually reads out of one record. Declared as
    # data rather than code so the canary asserts the SAME field names the
    # parser depends on; a hand-written per-connector probe would drift from
    # the parser it is meant to protect.
    #
    # Empty tuple = this connector opts out (its store is not line-oriented
    # JSON), and `format_health` reports `absent` rather than guessing.
    FORMAT_KEYS: tuple[str, ...] = ()

    def format_health(self, path_override: str | None = None) -> FormatHealth:
        """I2 — sample real records and assert the shape we parse.

        Default implementation covers every connector whose store is
        newline-delimited JSON. A connector backed by SQLite overrides this.

        Two design points, both learned by running it against the live store:

        * Files come from `snapshot_roots()`, not `discover().path`. Codex
          reports its PARENT directory as the source path so a copier captures
          both rollout roots, but that parent also holds `hook-log.jsonl`,
          which `scan` never reads. Sampling by `discover().path` reported a
          healthy Codex as `drifted` — the canary was reading a file the
          parser does not.

        * The verdict is MAJORITY over sampled records, not the first record.
          OMP's newest transcript opens with a header record carrying `type`
          but no `timestamp`; 963 of its 964 records are well-formed. Judging
          on record one called a healthy source drifted. A format change moves
          the whole file, not one line, so a single odd record is noise and a
          majority miss is signal.
        """
        if not self.FORMAT_KEYS:
            return FormatHealth(self.SOURCE_ID, "absent", "connector declares no format keys")

        import json as _json

        roots = self.snapshot_roots(path_override)
        if not roots:
            info = self.discover(path_override)
            return FormatHealth(self.SOURCE_ID, "absent", info.error or "source unavailable")

        files: list[Path] = []
        for root in roots:
            try:
                files.extend(p for p in root.rglob("*.jsonl") if p.is_file())
            except OSError as exc:
                return FormatHealth(self.SOURCE_ID, "absent", f"cannot list {root}: {exc}")
        if not files:
            return FormatHealth(self.SOURCE_ID, "absent", "no .jsonl under source roots")

        try:
            newest = max(files, key=lambda p: p.stat().st_mtime)
        except OSError as exc:
            return FormatHealth(self.SOURCE_ID, "absent", f"cannot stat: {exc}")

        SAMPLE = 200
        matched = 0
        checked = 0
        missing_counts: dict[str, int] = {}
        try:
            with newest.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = _json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    checked += 1
                    missing = [k for k in self.FORMAT_KEYS if k not in obj]
                    if missing:
                        for k in missing:
                            missing_counts[k] = missing_counts.get(k, 0) + 1
                    else:
                        matched += 1
                    if checked >= SAMPLE:
                        break
        except OSError as exc:
            return FormatHealth(self.SOURCE_ID, "absent", f"cannot read {newest}: {exc}")

        if checked == 0:
            return FormatHealth(self.SOURCE_ID, "absent", f"no parsable record in {newest}")
        if matched * 2 > checked:
            return FormatHealth(self.SOURCE_ID, "ok", f"{matched}/{checked} records", str(newest))
        worst = ", ".join(
            f"{k} (missing in {v}/{checked})"
            for k, v in sorted(missing_counts.items(), key=lambda kv: -kv[1])
        )
        return FormatHealth(
            self.SOURCE_ID, "drifted",
            f"only {matched}/{checked} records carry the parsed keys; {worst}",
            str(newest),
        )
