"""Confidence-tiered correlation of SourceRecords into per-project session
timelines and router links.

Grouping strategy (see module docstring in ``protocol.py`` for record shape):

* omp / pi / claude / codex — JSONL harnesses where the source file itself
  already delimits a session. Group by ``source_file``.
* opencode — a single shared SQLite DB, so ``source_file`` is useless as a
  session key. ``session_start`` records carry the real native session id
  directly on ``native_id``; ``message``/``tool_call``/``error`` records
  from the current connector carry no session linkage at all. Rather than
  merging every opencode message into one giant "session" (which would be
  wrong), records without a resolvable session id each become their own
  deterministic singleton group.
* nine_router — ``router_request`` records are never grouped into sessions;
  they are matched against session groups via :func:`correlate_records`'s
  tiered linking and reported separately as ``router_links``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .logging_util import get_logger
from .protocol import SourceRecord
from .redact import redact, redact_value

log = get_logger("correlate")

# Match id-shaped raw_meta keys after normalizing away separators/case, e.g.
# request_id, requestId, session-id, nativeId, connection_id, id.
def _is_id_shaped_key(key: str) -> bool:
    norm = re.sub(r"[_\-]", "", str(key)).lower()
    return norm == "id" or norm.endswith("id")


JSONL_FILE_KEYED_SOURCES = {"omp", "pi", "claude", "codex"}
ROUTER_SOURCE_ID = "nine_router"
ROUTER_RECORD_TYPE = "router_request"
# Session-bearing record types per spec: file_edit/session_end are
# informational only and never participate in session grouping or
# turn counting. Only message records increment turn_count. Both
# "tool" and "tool_call" are accepted — connectors emit "tool_call"
# but the spec text refers to it as "tool".
SESSION_BEARING_TYPES = {"session_start", "message", "tool_call", "tool", "error"}
TURN_COUNTED_TYPES = {"message"}


def _id_shaped_values(raw_meta: dict) -> set[str]:
    """Values from raw_meta keys that look like an identifier (id, *_id,
    *Id, request_id, session_id, connection_id — case/separator
    insensitive), including one level of nested mappings. Excludes
    scalar noise like provider/status/token counts."""
    ids: set[str] = set()
    if not raw_meta:
        return ids
    for k, v in raw_meta.items():
        if isinstance(v, dict):
            ids |= _id_shaped_values(v)
            continue
        if not _is_id_shaped_key(k):
            continue
        if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v):
            ids.add(str(v))
    return ids

_GroupKey = tuple[str, str]  # (source_id, key)


@dataclass
class Link:
    left_id: str
    right_id: str
    tier: int
    confidence: str
    reason: str


@dataclass
class SessionBucket:
    source_id: str
    native_id: str
    project_path: str
    started_ms: int
    ended_ms: int
    model: str
    git_branch: str
    turn_count: int
    raw_path: str
    group_key: str = ""

@dataclass
class CorrelateResult:
    sessions: list[SessionBucket] = field(default_factory=list)
    router_links: list[Link] = field(default_factory=list)
    unmatched_router: int = 0
    project_paths: set[str] = field(default_factory=set)


def normalize_fingerprint(text: str) -> str:
    """Collapse whitespace and lowercase, for loose content matching."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class _SessionGroup:
    """Internal working accumulator — richer than the public SessionBucket."""
    source_id: str
    native_id: str
    raw_path: str = ""
    project_path: str = ""
    started_ms: int = 0
    ended_ms: int = 0
    model: str = ""
    git_branch: str = ""
    turn_count: int = 0
    member_native_ids: set[str] = field(default_factory=set)
    member_raw_ids: set[str] = field(default_factory=set)
    fingerprints: set[str] = field(default_factory=set)
    _absorbed_count: int = 0
    _session_start_ts: int | None = None

    def absorb(self, rec: SourceRecord) -> None:
        if not self.project_path and rec.project_path:
            self.project_path = rec.project_path
        if not self.model and rec.model:
            self.model = rec.model
        if not self.git_branch and rec.git_branch:
            self.git_branch = rec.git_branch
        if not self.raw_path and rec.source_file:
            self.raw_path = rec.source_file
        if self._absorbed_count == 0 or rec.timestamp_ms < self.started_ms:
            self.started_ms = rec.timestamp_ms
        if rec.timestamp_ms > self.ended_ms:
            self.ended_ms = rec.timestamp_ms
        if rec.record_type in TURN_COUNTED_TYPES:
            self.turn_count += 1
        self._absorbed_count += 1
        if rec.native_id:
            self.member_native_ids.add(rec.native_id)
        for id_val in _id_shaped_values(rec.raw_meta):
            self.member_raw_ids.add(id_val)
        if rec.content_excerpt:
            fp = normalize_fingerprint(rec.content_excerpt)
            if fp:
                self.fingerprints.add(fp)

    def to_bucket(self) -> SessionBucket:
        return SessionBucket(
            source_id=self.source_id,
            native_id=self.native_id,
            project_path=self.project_path,
            started_ms=self.started_ms,
            ended_ms=self.ended_ms,
            model=self.model,
            git_branch=self.git_branch,
            turn_count=self.turn_count,
            raw_path=self.raw_path,
        )
_SESSION_KEY_NAMES = {"sessionid", "nativesessionid", "session"}


def _find_session_id(raw_meta: dict) -> str | None:
    """Look for a session-specific id in raw_meta only (never an arbitrary
    id/request id) after separator/case normalization: session_id,
    sessionId, native_session_id, session — but not message_id, id,
    request_id, connection_id, etc."""
    if not raw_meta:
        return None
    for k, v in raw_meta.items():
        if isinstance(v, dict):
            found = _find_session_id(v)
            if found:
                return found
            continue
        norm = re.sub(r"[_\-]", "", str(k)).lower()
        if norm in _SESSION_KEY_NAMES:
            if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v):
                return str(v)
    return None


def _session_group_key(rec: SourceRecord) -> _GroupKey | None:
    """Group key for a session-bearing record, or None for router requests."""
    if rec.source_id == ROUTER_SOURCE_ID or rec.record_type == ROUTER_RECORD_TYPE:
        return None
    if rec.record_type not in SESSION_BEARING_TYPES:
        # file_edit/session_end etc. are informational only — never
        # create or join a session group, never counted as turns.
        return None

    if rec.source_id in JSONL_FILE_KEYED_SOURCES:
        # One JSONL file == one session for these harnesses.
        return (rec.source_id, rec.source_file or rec.native_id)

    if rec.source_id == "opencode":
        if rec.record_type == "session_start" and rec.native_id:
            return (rec.source_id, rec.native_id)
        # opencode.py DOES propagate session linkage: it sets
        # raw_meta["session_id"] on every message record (opencode.py:116),
        # which _find_session_id resolves below. Measured on live data:
        # 1,283 of 1,283 opencode turns group under 79 real sessions, zero
        # __ungrouped__ fallbacks. (An earlier comment here claimed the
        # connector never propagated it; that was true of an older connector
        # and is no longer.)
        #
        # The fallback is retained because it is still reachable — a record
        # whose raw_meta lacks the key must never be merged on a guess, so it
        # keys as its own deterministic group rather than silently joining a
        # neighbouring session.
        session_native = _find_session_id(rec.raw_meta)
        if session_native:
            return (rec.source_id, str(session_native))
        return (rec.source_id, f"__ungrouped__{rec.native_id or rec.timestamp_ms}")

    # Unknown source: fall back to source_file, then native_id.
    return (rec.source_id, rec.source_file or rec.native_id)


def _extract_router_id_candidates(rec: SourceRecord) -> set[str]:
    """Every id-shaped value on a record that might overlap a session/turn id."""
    ids: set[str] = set()
    if rec.native_id:
        ids.add(str(rec.native_id))
    ids |= _id_shaped_values(rec.raw_meta)
    return ids


def _stable_pick(matches: list[tuple[_GroupKey, _SessionGroup]]) -> tuple[_GroupKey, _SessionGroup]:
    """Deterministic tie-break when multiple session groups match: earliest
    started_ms, then lexicographic group key."""
    return sorted(matches, key=lambda m: (m[1].started_ms, m[0][0], m[0][1]))[0]


def _build_router_match_indexes(groups: dict[_GroupKey, _SessionGroup], window_ms: int):
    """Build indexes so a single router match costs O(log N + matches) instead
    of O(|groups|).

    Returns:
      - id_index: native-id candidate -> list of (gkey, group) preserving first
        insertion order. Multiple groups sharing the same id stay co-located for
        the _stable_pick tie-break.
      - tier2_index: model -> list of (mid_ms, gkey) sorted asc
      - tier3_index: fingerprint -> list of (mid_ms, gkey) sorted asc
      - group_attrs: gkey -> (model, project_path, mid_ms, started_ms, ended_ms)
    """
    id_index: dict[str, list] = {}
    tier2_index: dict[str, list] = {}
    tier3_index: dict[str, list] = {}
    group_attrs: dict = {}

    def _add_id(value, gkey):
        if not value:
            return
        s = id_index.get(value)
        if s is None:
            id_index[value] = [gkey]
        elif gkey not in s:
            s.append(gkey)

    for gkey, group in groups.items():
        mid = (group.started_ms + (group.ended_ms or group.started_ms)) // 2
        project = group.project_path or ""
        model = group.model or ""
        # Tier 1 ids: native session id + member turn ids + raw_meta ids.
        # Fingerprints are NOT in id_index — they belong in tier3_index only,
        # because fingerprint hits are tier 3 by semantic contract.
        if group.native_id:
            _add_id(group.native_id, gkey)
        for mid_id in group.member_native_ids:
            _add_id(mid_id, gkey)
        for raw_id in group.member_raw_ids:
            _add_id(raw_id, gkey)
        # Tier 2: keyed by model ALONE. The project half of the T2 rule is
        # satisfied by the matched GROUP carrying a project, not by the router
        # record — 9router's usageHistory has no cwd/project column at all, so
        # keying on (model, router.project_path) could never match anything.
        if model:
            tier2_index.setdefault(model, [[], []])
            tier2_index[model][0].append(mid)
            tier2_index[model][1].append((mid, gkey))
        # Tier 3: keyed by fingerprint ALONE, same reasoning. A group with no
        # resolvable project is still excluded — an unattributable link cannot
        # enrich anything and would only add noise.
        if project:
            for fp in group.fingerprints:
                tier3_index.setdefault(fp, [[], []])
                tier3_index[fp][0].append(mid)
                tier3_index[fp][1].append((mid, gkey))
        # group_attrs stores: (model, project, mid_ms, started_ms, ended_ms, group_ref)
        # The group_ref is used to read `group.native_id` for left_id construction,
        # matching the original semantics (left_id = source_id:group.native_id).
        group_attrs[gkey] = (model, project, mid, group.started_ms, group.ended_ms, group)
    # Sort pairs by mid in place so bisect can use parallel mids arrays (no
    # per-router list allocation in the hot path).
    #
    # Each bucket becomes (mids, pairs, max_half). `max_half` is the largest
    # midpoint-to-edge distance in that bucket, computed ONCE here. The matcher
    # widens its bisect by it so no in-span candidate is missed, without
    # rescanning the bucket on every router record (that would be O(routers x
    # sessions) — 78k x 7k on the shipping dataset).
    #
    # Use max(mid-start, end-mid), not (end-start)//2: floor division can be
    # 1 ms short on odd-length spans and drop a boundary candidate.
    def _finalize(index):
        for key in list(index.keys()):
            _mids, pairs = index[key]
            order = sorted(range(len(pairs)), key=lambda i: pairs[i][0])
            spairs = [pairs[i] for i in order]
            max_half = 0
            for mid, gk in spairs:
                a = group_attrs.get(gk)
                if a and a[3] is not None and a[4] is not None:
                    half = max(mid - a[3], a[4] - mid)
                    if half > max_half:
                        max_half = half
            index[key] = ([p[0] for p in spairs], spairs, max_half)

    _finalize(tier2_index)
    _finalize(tier3_index)
    return {
        "id_index": id_index,
        "tier2_index": tier2_index,
        "tier3_index": tier3_index,
        "group_attrs": group_attrs,
    }


def _left_id(gkey, indexes):
    """Build left_id from the matched group's stored native_id, matching the
    original semantics (left_id = source_id:group.native_id, not :grouping_key).
    The 6th slot of group_attrs is the live _SessionGroup reference."""
    return f"{gkey[0]}:{indexes['group_attrs'][gkey][5].native_id}"


def _stable_pick_groups(gkeys, indexes):
    """Pick the deterministic best group from a candidate set per _stable_pick.
    Tie-break: earliest started_ms, then lexicographic (source_id, key)."""
    if not gkeys:
        return None
    return sorted(
        gkeys,
        key=lambda gk: (indexes["group_attrs"][gk][3], gk[0], gk[1]),
    )[0]


def _unambiguous_project(gkeys, indexes) -> str | None:
    """Return the single project every candidate agrees on, else None.

    This is the anti-false-match guard. A router request whose time window
    spans session groups from two different projects cannot be attributed
    to either without guessing, and per SPEC-dreamy.md §3.3 a wrong link is
    strictly worse than no link: it merges unrelated work episodes into a
    confidently wrong resumption prompt. Such a request stays T4 unlinked.
    """
    projects = {indexes["group_attrs"][gk][1] for gk in gkeys}
    projects.discard("")
    if len(projects) != 1:
        return None
    return next(iter(projects))


def _window_candidates(bucket, ts_ms: int, window_ms: int, group_attrs=None):
    """Candidate gkeys whose SESSION SPAN is within `window_ms` of the router ts.

    Distance is measured to the interval `[started_ms, ended_ms]`, not to the
    midpoint. Midpoint distance is wrong in both directions:

      - a request landing INSIDE a long session is rejected when the session is
        longer than 2*window (measured: 16,662 such requests at a 30s window,
        median midpoint distance ~6,114s — all of them zero distance from the
        span itself);
      - a request well outside a long session is accepted whenever the midpoint
        happens to sit near it.

    The bisect stays keyed on midpoint as a cheap PREFILTER. It is widened by
    the longest span in the bucket so no in-span candidate is missed, then the
    exact interval test below decides. Correctness comes from the test, speed
    from the prefilter.
    """
    from bisect import bisect_left, bisect_right

    # Buckets are (mids, pairs, max_half); `max_half` is precomputed once at
    # index build. Legacy 2-tuples are still accepted for direct callers.
    if len(bucket) == 3:
        mids, pairs, max_half = bucket
    else:
        mids, pairs = bucket
        max_half = 0

    if group_attrs is None:
        lo = bisect_left(mids, ts_ms - window_ms)
        hi = bisect_right(mids, ts_ms + window_ms)
        return [pairs[i][1] for i in range(lo, hi)]

    reach = window_ms + max_half
    lo = bisect_left(mids, ts_ms - reach)
    hi = bisect_right(mids, ts_ms + reach)

    out = []
    for i in range(lo, hi):
        gk = pairs[i][1]
        a = group_attrs.get(gk)
        if not a:
            continue
        st, en = a[3], a[4]
        if st is None:
            continue
        if en is None:
            en = st
        # Distance to the interval: 0 when contained.
        if ts_ms < st:
            dist = st - ts_ms
        elif ts_ms > en:
            dist = ts_ms - en
        else:
            dist = 0
        if dist <= window_ms:
            out.append(gk)
    return out


def _match_router_indexed(
    router: SourceRecord,
    indexes: dict,
    window_ms: int,
) -> Link | None:
    router_ids = _extract_router_id_candidates(router)
    router_fp = normalize_fingerprint(router.content_excerpt) if router.content_excerpt else ""
    window_s = window_ms // 1000

    # Tier 1 — exact native/request/session key overlap. Confidence "exact"
    # per SPEC-dreamy.md §3.3. Fingerprints are NOT considered at tier 1.
    tier1_gkeys = []
    seen = set()
    matched_id = ""
    for rid in router_ids:
        for gk in indexes["id_index"].get(rid, ()):
            if gk not in seen:
                seen.add(gk)
                tier1_gkeys.append(gk)
                if not matched_id:
                    matched_id = rid
    if tier1_gkeys:
        gkey = _stable_pick_groups(tier1_gkeys, indexes)
        project = indexes["group_attrs"][gkey][1] or "(unresolved project)"
        return Link(
            left_id=_left_id(gkey, indexes), right_id=router.native_id,
            tier=1, confidence="exact",
            reason=f"exact native id overlap on {matched_id} → session in {project}",
        )

    # Tier 2 — same project + bounded timestamp + same model. Confidence "high"
    # per DESIGN.md §3.3, which also sets the window default at 30s.
    # The model index is keyed by model alone; the project constraint is
    # enforced by requiring every in-window candidate to name the same project.
    if router.model:
        bucket = indexes["tier2_index"].get(router.model)
        if bucket:
            candidates = _window_candidates(bucket, router.timestamp_ms, window_ms, indexes["group_attrs"])
            project = _unambiguous_project(candidates, indexes)
            if project:
                gkey = _stable_pick_groups(
                    [gk for gk in candidates if indexes["group_attrs"][gk][1] == project],
                    indexes,
                )
                return Link(
                    left_id=_left_id(gkey, indexes), right_id=router.native_id,
                    tier=2, confidence="high",
                    reason=f"model={router.model} + project={project} within {window_s}s",
                )

    # Tier 3 — normalized prompt fingerprint + project + bounded window.
    # Confidence "medium". Same ambiguity guard.
    if router_fp:
        bucket = indexes["tier3_index"].get(router_fp)
        if bucket:
            candidates = _window_candidates(bucket, router.timestamp_ms, window_ms, indexes["group_attrs"])
            project = _unambiguous_project(candidates, indexes)
            if project:
                gkey = _stable_pick_groups(
                    [gk for gk in candidates if indexes["group_attrs"][gk][1] == project],
                    indexes,
                )
                return Link(
                    left_id=_left_id(gkey, indexes), right_id=router.native_id,
                    tier=3, confidence="medium",
                    reason=f"prompt fingerprint + project={project} within {window_s}s",
                )

    # Tier 4 — no defensible link. Retained in full, never force-joined.
    return None


def correlate_records(records: list[SourceRecord], window_ms: int = 30_000) -> CorrelateResult:
    """Group SourceRecords into per-source session buckets and link router
    requests to sessions at decreasing confidence tiers. Deterministic for a
    given input list: re-running with the identical record order always
    produces identical output. "First non-empty" fields (project_path,
    model, git_branch, raw_path) follow the caller's supplied record order
    by design (per spec) and feed into router tier matching, so shuffling
    input order with conflicting metadata across a group can change tier
    outcomes. started_ms/ended_ms, session_start selection, and session
    output ordering are independent of input order."""
    result = CorrelateResult()
    if not records:
        return result

    groups: dict[_GroupKey, _SessionGroup] = {}
    router_records: list[SourceRecord] = []

    # "First non-empty" fields (project_path, model, git_branch, raw_path)
    # absorbed by _SessionGroup honor the caller's supplied record order,
    # per spec: "project_path = first non-empty project_path on records
    # in group". started_ms/ended_ms and session_start tie-break are
    # independently order-independent (explicit min/max + timestamp compare).
    for rec in records:
        if rec.source_id == ROUTER_SOURCE_ID:
            if rec.record_type == ROUTER_RECORD_TYPE:
                router_records.append(rec)
            # nine_router keeps router_requests only — any other record
            # type from this source is dropped, not counted as unmatched.
            continue
        if rec.record_type == ROUTER_RECORD_TYPE:
            router_records.append(rec)
            continue
        if rec.record_type not in SESSION_BEARING_TYPES:
            # file_edit/session_end etc. are informational only — never
            # create/join a session group and never counted as router-unmatched.
            continue

        gkey = _session_group_key(rec)
        if gkey is None:
            continue

        source_id, key = gkey
        group = groups.get(gkey)
        if group is None:
            group = _SessionGroup(source_id=source_id, native_id=key)
            groups[gkey] = group

        if rec.record_type == "session_start" and rec.native_id:
            # Order-independent: keep whichever session_start is earliest
            # by timestamp, not whichever was processed last.
            if group._session_start_ts is None or rec.timestamp_ms < group._session_start_ts:
                group.native_id = rec.native_id
                group._session_start_ts = rec.timestamp_ms

        group.absorb(rec)

    for gkey in sorted(groups.keys()):
        group = groups[gkey]
        bucket = group.to_bucket()
        bucket.group_key = gkey[1]
        result.sessions.append(bucket)
        if bucket.project_path:
            result.project_paths.add(bucket.project_path)

    log.info(
        "grouped records",
        session_count=len(result.sessions),
        router_count=len(router_records),
    )

    indexes = _build_router_match_indexes(groups, window_ms)
    for router in sorted(router_records, key=lambda r: (r.timestamp_ms, r.native_id, r.source_file)):
        link = _match_router_indexed(router, indexes, window_ms)
        if link is None:
            result.unmatched_router += 1
            continue
        result.router_links.append(link)

    log.info(
        "router linking done",
        linked=len(result.router_links),
        unmatched=result.unmatched_router,
    )

    return result

@dataclass
class PersistStats:
    projects: int = 0
    sessions: int = 0
    turns: int = 0
    skipped_sessions: int = 0
    links: int = 0
    skipped_links: int = 0
    project_ids: dict[str, str] = field(default_factory=dict)

def persist_correlated(store, records: list[SourceRecord], result: CorrelateResult) -> PersistStats:
    """Persist correlated projects, sessions and turns to the state DB.

    Production persistence for Phase 1. Called from ``run.run_pipeline`` and by
    acceptance capture so evidence exercises the same code path users run.

    Turn mapping relies on the invariant that a bucket's grouping key is
    ``(bucket.source_id, bucket.group_key)`` — the exact tuple
    :func:`_session_group_key` returns for every record in that group.

    Idempotent: ``upsert_project``/``upsert_session`` upsert on a stable id and
    ``insert_turn`` uses INSERT OR IGNORE on a content-derived id, so a second
    run over identical records adds zero rows.
    """
    import json

    stats = PersistStats()

    project_ids: dict[str, str] = {}
    for bucket in result.sessions:
        path = bucket.project_path
        if not path or path in project_ids:
            continue
        spans = [b for b in result.sessions if b.project_path == path]
        project_ids[path] = store.upsert_project(
            path=path,
            name=path.rstrip("/").rsplit("/", 1)[-1] or path,
            first_ms=min(b.started_ms for b in spans),
            last_ms=max(b.ended_ms for b in spans),
        )
    stats.projects = len(project_ids)
    stats.project_ids = dict(project_ids)

    native_to_sid: dict[tuple[str, str], str] = {}
    gkey_to_sid: dict[_GroupKey, str] = {}
    for bucket in result.sessions:
        project_id = project_ids.get(bucket.project_path or "")
        if not project_id:
            # No resolvable project: persisting would create a dangling
            # sessions.project_id reference. Skip the session — and, because
            # its group key never enters gkey_to_sid, its turns are skipped too.
            stats.skipped_sessions += 1
            continue
        sid = store.upsert_session(
            project_id=project_id,
            source_id=bucket.source_id,
            native_id=bucket.native_id,
            started_ms=bucket.started_ms,
            ended_ms=bucket.ended_ms,
            model=bucket.model,
            git_branch=bucket.git_branch,
            raw_path=bucket.raw_path,
            record_count=bucket.turn_count,
        )
        gkey_to_sid[(bucket.source_id, bucket.group_key)] = sid
        # Links carry left_id as "<source_id>:<group.native_id>", which is the
        # same (source_id, native_id) pair upsert_session keys its stable id on.
        native_to_sid[(bucket.source_id, bucket.native_id)] = sid
        stats.sessions += 1

    for rec in records:
        if rec.record_type not in TURN_COUNTED_TYPES:
            continue
        gkey = _session_group_key(rec)
        if gkey is None:
            continue
        sid = gkey_to_sid.get(gkey)
        if sid is None:
            continue
        stats.turns += store.insert_turn(
            session_id=sid,
            source_id=rec.source_id,
            ts_ms=rec.timestamp_ms,
            role=rec.role,
            model=rec.model,
            # `redact()` BEFORE normalisation, matching every sibling field on
            # this call. This was the one unredacted argument, and it is how a
            # live auth token reached `turns.content_fingerprint`: connectors
            # redact excerpts at ingest, but a fingerprint derived from an
            # excerpt re-entered the write path without passing the boundary
            # again. Redacting here makes the guarantee positional — every
            # value bound by this statement has crossed the boundary — instead
            # of depending on an upstream call that a future connector may not
            # make.
            fingerprint=normalize_fingerprint(redact(rec.content_excerpt or "")),
            tool_name=rec.tool_name,
            file_paths_json=json.dumps(redact_value(rec.file_paths or [])),
            error_text=redact(rec.error_text or ""),
            raw_meta_json=json.dumps(redact_value(rec.raw_meta or {})),
        )

    # Correlation links — router_requests.linked_session_id / tier / reason.
    # A link whose session was skipped (unresolvable project) is counted as
    # skipped rather than written against a dangling session reference.
    for link in result.router_links:
        source_id, _, native_id = link.left_id.partition(":")
        sid = native_to_sid.get((source_id, native_id))
        if sid is None:
            stats.skipped_links += 1
            continue
        written = store.link_router_request(
            source_id=ROUTER_SOURCE_ID,
            native_id=link.right_id,
            session_id=sid,
            tier=link.tier,
            confidence=link.confidence,
            reason=link.reason,
        )
        if written:
            stats.links += 1
        else:
            stats.skipped_links += 1

    store.conn.commit()
    log.info(
        "persisted correlated",
        projects=stats.projects,
        sessions=stats.sessions,
        turns=stats.turns,
        links=stats.links,
        skipped_links=stats.skipped_links,
        skipped_sessions=stats.skipped_sessions,
    )
    return stats
