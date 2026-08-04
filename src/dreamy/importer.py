"""I5 — archival import of a `dreamy bundle` HTML file.

Reads the machine-readable payload embedded by `bundle.export_project_bundle`
and records it in the v8 archival tables.

This is an ARCHIVAL SNAPSHOT READER, not a project restore, and the
distinction is load-bearing rather than pedantic. The exported payload carries
per-bundle opaque ids and timestamps only — no project link, no source id, no
role, no model, no content, and no canonical id. From that a receiver can
reconstruct a timeline and volumes; it cannot attribute a turn to a project,
recompute `turns.id` (derived from the raw fingerprint, which never leaves the
sending machine), correlate across harnesses, or feed analysis. Claiming
otherwise would be the same overclaim class this codebase has already had to
correct several times.

Imported rows therefore live in `imported_*` tables that share no key space
with `sessions`/`turns`. An import can never collide with, overwrite, or
resurrect a locally-ingested row, because the two never meet.

=== TRUST BOUNDARY ===

A bundle arrives from another machine and is UNTRUSTED INPUT. Every value is
validated before a single row is written, and any violation rejects the whole
file:

* fail CLOSED, never skip-and-continue. An earlier draft warned about
  malformed rows and committed the rest, which produces a manifest whose
  counts describe data that was never checked — an archive that lies is worse
  than an import that refuses.
* validate BEFORE `BEGIN`. Nothing is written until the entire payload has
  passed, so a rejection needs no rollback to be clean.
* bound everything. File size, row counts, string lengths, and integer ranges
  all have explicit caps, because "the sender is a copy of us" is an
  assumption a public artifact cannot make.
* no coercion of hostile values. `str(x)` on an arbitrary object would happily
  stringify a dict; ids must already BE 32-hex strings, and integers must be
  real ints (bools rejected — `True` is an `int` in Python and would sail
  through a naive check).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import Store

# The payload is emitted by `bundle._json_for_script`, which escapes `<`, `>`,
# and `&` as `\uXXXX`. A `</script>` therefore cannot appear inside the JSON,
# so a non-greedy match to the first closing tag is exact rather than
# best-effort.
_PAYLOAD_RE = re.compile(
    r'<script type="application/json" id="dreamy-bundle">(.*?)</script>',
    re.DOTALL,
)

# Every version this importer understands. A bundle from the future is
# REFUSED, never partially read: a payload whose shape changed could import
# silently wrong data.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

# Bounds. Sized well above any plausible real bundle (the largest project on
# the development machine exports 4,267 sessions / 28,286 turns) and far below
# anything that could exhaust memory.
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_SESSIONS = 200_000
MAX_TURNS_TOTAL = 5_000_000
MAX_TURNS_PER_SESSION = 200_000
MAX_NAME_LEN = 512
# Milliseconds. Lower bound excludes 0/negative; upper is year ~2500, which is
# comfortably absurd for a session timestamp but still an int SQLite stores.
MIN_MS = 1
MAX_MS = 16_725_225_600_000
MAX_RECORD_COUNT = 100_000_000

_EXPORT_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_BUNDLE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")

_SESSION_KEYS = {"export_id", "started_ms", "ended_ms", "record_count", "turns"}
_TURN_KEYS = {"export_id", "timestamp_ms"}


class BundleFormatError(ValueError):
    """The file is not a readable, well-formed dreamy bundle."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`object_pairs_hook` that REJECTS duplicate keys.

    Plain `json.loads` keeps the LAST value for a repeated key, so
    `{"schema_version":1,"schema_version":99}` parses as 99 while a reader
    eyeballing the file sees 1. That is a validator-bypass primitive, not a
    curiosity: every check below runs on the surviving value. Duplicate keys
    are never produced by our own writer, so rejecting them costs nothing.
    """
    seen: set[str] = set()
    for k, _ in pairs:
        if k in seen:
            raise BundleFormatError(f"payload contains a duplicate JSON key: {k!r}")
        seen.add(k)
    return dict(pairs)


@dataclass
class ImportResult:
    bundle_id: str = ""
    project_name: str = ""
    exported_ms: int = 0
    sessions_written: int = 0
    turns_written: int = 0
    already_imported: bool = False
    warnings: list[str] = field(default_factory=list)


def _require_int(value: Any, field_name: str, *, lo: int, hi: int, allow_none: bool = False) -> int | None:
    """An int in [lo, hi]. Bools are REJECTED: `isinstance(True, int)` is True."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BundleFormatError(f"{field_name}: expected an integer, got {type(value).__name__}")
    if not (lo <= value <= hi):
        raise BundleFormatError(f"{field_name}: {value} out of range [{lo}, {hi}]")
    return value


def _require_id(value: Any, field_name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise BundleFormatError(f"{field_name}: expected a string id, got {type(value).__name__}")
    if not pattern.match(value):
        raise BundleFormatError(f"{field_name}: {value!r} is not a 32-character lowercase hex id")
    return value


def parse_bundle(path: Path) -> dict[str, Any]:
    """Extract and FULLY validate the embedded payload.

    Returns a payload whose session/turn structure is guaranteed well-formed:
    every id is 32-hex and unique, every integer is in range, no unknown keys,
    and all bounds respected. Raises `BundleFormatError` otherwise.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise BundleFormatError(f"cannot stat {p}: {exc}") from exc
    if size > MAX_FILE_BYTES:
        raise BundleFormatError(
            f"{p}: {size} bytes exceeds the {MAX_FILE_BYTES}-byte import limit"
        )
    try:
        html = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BundleFormatError(f"cannot read {p}: {exc}") from exc

    # findall, not search: a file carrying TWO payload elements is ambiguous —
    # `search` would silently import the first while a browser or a careless
    # reader might act on the second. Ambiguity at a trust boundary is a
    # rejection, not a preference.
    found = _PAYLOAD_RE.findall(html)
    if not found:
        raise BundleFormatError(
            f"{p} contains no dreamy bundle payload "
            "(exported by a version before machine-readable bundles?)"
        )
    if len(found) > 1:
        raise BundleFormatError(
            f"{p}: {len(found)} dreamy-bundle payload elements found; expected exactly one"
        )
    raw_payload = found[0]
    try:
        payload = json.loads(raw_payload, object_pairs_hook=_no_duplicate_keys)
    except BundleFormatError:
        raise
    except ValueError as exc:
        raise BundleFormatError(f"{p}: payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BundleFormatError(f"{p}: payload is not an object")

    version = payload.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise BundleFormatError(
            f"{p}: unsupported bundle schema_version {version!r} "
            f"(this build reads {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    _require_id(payload.get("bundle_id"), "bundle_id", _BUNDLE_ID_RE)
    _require_int(payload.get("exported_ms"), "exported_ms", lo=MIN_MS, hi=MAX_MS)

    project = payload.get("project")
    if not isinstance(project, dict):
        raise BundleFormatError(f"{p}: 'project' is not an object")
    name = project.get("name")
    if name is not None and not isinstance(name, str):
        raise BundleFormatError("project.name: expected a string")
    if isinstance(name, str) and len(name) > MAX_NAME_LEN:
        raise BundleFormatError(
            f"project.name: {len(name)} characters exceeds the {MAX_NAME_LEN} limit"
        )

    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        raise BundleFormatError(f"{p}: 'sessions' is not a list")
    if len(sessions) > MAX_SESSIONS:
        raise BundleFormatError(
            f"{p}: {len(sessions)} sessions exceeds the {MAX_SESSIONS} limit"
        )

    seen_sessions: set[str] = set()
    seen_turns: set[str] = set()
    total_turns = 0
    for i, s in enumerate(sessions):
        where = f"sessions[{i}]"
        if not isinstance(s, dict):
            raise BundleFormatError(f"{where}: not an object")
        unknown = set(s) - _SESSION_KEYS
        if unknown:
            raise BundleFormatError(f"{where}: unexpected key(s) {sorted(unknown)}")
        sid = _require_id(s.get("export_id"), f"{where}.export_id", _EXPORT_ID_RE)
        if sid in seen_sessions:
            raise BundleFormatError(f"{where}.export_id: duplicate id {sid}")
        seen_sessions.add(sid)
        _require_int(s.get("started_ms"), f"{where}.started_ms", lo=MIN_MS, hi=MAX_MS, allow_none=True)
        _require_int(s.get("ended_ms"), f"{where}.ended_ms", lo=MIN_MS, hi=MAX_MS, allow_none=True)
        _require_int(
            s.get("record_count"), f"{where}.record_count", lo=0, hi=MAX_RECORD_COUNT, allow_none=True
        )

        turns = s.get("turns")
        if turns is None:
            turns = []
        if not isinstance(turns, list):
            raise BundleFormatError(f"{where}.turns: not a list")
        if len(turns) > MAX_TURNS_PER_SESSION:
            raise BundleFormatError(
                f"{where}.turns: {len(turns)} exceeds the {MAX_TURNS_PER_SESSION} per-session limit"
            )
        total_turns += len(turns)
        if total_turns > MAX_TURNS_TOTAL:
            raise BundleFormatError(f"{p}: total turns exceeds the {MAX_TURNS_TOTAL} limit")
        for j, t in enumerate(turns):
            tw = f"{where}.turns[{j}]"
            if not isinstance(t, dict):
                raise BundleFormatError(f"{tw}: not an object")
            unknown_t = set(t) - _TURN_KEYS
            if unknown_t:
                raise BundleFormatError(f"{tw}: unexpected key(s) {sorted(unknown_t)}")
            tid = _require_id(t.get("export_id"), f"{tw}.export_id", _EXPORT_ID_RE)
            if tid in seen_turns:
                raise BundleFormatError(f"{tw}.export_id: duplicate id {tid}")
            seen_turns.add(tid)
            _require_int(
                t.get("timestamp_ms"), f"{tw}.timestamp_ms", lo=MIN_MS, hi=MAX_MS, allow_none=True
            )
    return payload


def import_bundle(store: Store, path: Path) -> ImportResult:
    """Record a validated bundle's session skeleton in the archival tables.

    Idempotent on `bundle_id`: re-importing the same file is a no-op rather
    than a duplicate or an error. Bundle ids are random per export, so two
    exports of the same project import as two independent snapshots — the
    deliberate cost of making bundles unlinkable, stated in
    `ReadStore.export_sessions` rather than papered over.
    """
    payload = parse_bundle(path)  # fully validated, or raised
    project = payload["project"]
    result = ImportResult(
        bundle_id=str(payload["bundle_id"]),
        project_name=str(project.get("name") or ""),
        exported_ms=int(payload["exported_ms"]),
    )

    # Canonical digest over the VALIDATED payload. `sort_keys` + tight
    # separators make it independent of key order and whitespace, so the same
    # logical bundle always digests identically.
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    existing = store.conn.execute(
        "SELECT session_count, turn_count, payload_sha256 FROM imported_bundles WHERE bundle_id=?",
        (result.bundle_id,),
    ).fetchone()
    if existing is not None:
        # Idempotency keys on CONTENT, not on the id alone. Returning
        # "already imported" purely because the id matched would accept a
        # tampered re-send — same bundle_id, different rows — and silently
        # keep the original data while reporting success. An id is a claim;
        # the digest is the evidence.
        if existing["payload_sha256"] != digest:
            raise BundleFormatError(
                f"bundle_id {result.bundle_id} was already imported with different content "
                f"(stored sha256 {existing['payload_sha256'][:16]}, this file {digest[:16]}). "
                "Refusing: an id collision or a tampered re-send must not be a no-op."
            )
        result.already_imported = True
        result.sessions_written = existing["session_count"]
        result.turns_written = existing["turn_count"]
        return result

    sessions = payload["sessions"]
    # Counts come from the VALIDATED payload, not from a running tally beside
    # `INSERT OR IGNORE`. A tally could disagree with the rows if an insert
    # were silently ignored; validation already guarantees ids are unique, so
    # the lengths are the truth.
    session_total = len(sessions)
    turn_total = sum(len(s.get("turns") or []) for s in sessions)

    try:
        store.conn.execute("BEGIN")
        store.conn.execute(
            "INSERT INTO imported_bundles(bundle_id, schema_version, exported_ms, "
            "imported_ms, source_file, project_name, session_count, turn_count, "
            "payload_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                result.bundle_id,
                int(payload["schema_version"]),
                result.exported_ms,
                int(time.time() * 1000),
                # Name only: a full path would re-import the sender's layout.
                Path(path).name[:MAX_NAME_LEN],
                result.project_name,
                session_total,
                turn_total,
                digest,
            ),
        )
        for s in sessions:
            sid = s["export_id"]
            store.conn.execute(
                "INSERT INTO imported_sessions("
                "bundle_id, export_id, started_ms, ended_ms, record_count) VALUES(?,?,?,?,?)",
                (
                    result.bundle_id,
                    sid,
                    s.get("started_ms"),
                    s.get("ended_ms"),
                    s.get("record_count") or 0,
                ),
            )
            for t in s.get("turns") or []:
                store.conn.execute(
                    "INSERT INTO imported_turns("
                    "bundle_id, export_id, session_export_id, timestamp_ms) VALUES(?,?,?,?)",
                    (result.bundle_id, t["export_id"], sid, t.get("timestamp_ms")),
                )
        store.conn.execute("COMMIT")
    except Exception:
        store.conn.execute("ROLLBACK")
        raise
    result.sessions_written = session_total
    result.turns_written = turn_total
    return result
