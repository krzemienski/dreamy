"""Deterministic session reconciliation analysis — no Agent SDK, no LLM calls.

Turns correlated session/turn evidence for a single project into an
``AnalysisResult``: a best-effort intent episode describing what the user
was trying to do and whether it looks finished, plus a small set of
mechanical findings (docs drift, tech debt markers, error bursts) and a
list of next-task seed strings.

Everything here is regex + arithmetic over strings and timestamps. There
is no model call anywhere in this module, and ``completion_status`` can
never be the literal ``"complete"`` — only an agent-verified pass (outside
this module) should ever assert that, so the deterministic heuristic caps
out at ``"unverified"``.

Input shape (see dreamy Phase 2 contract)
-------------------------------------------------------------
``sessions`` — a list of session-like objects for the project, each
exposing ``started_ms`` / ``ended_ms`` (dataclass attributes or dict
keys; both are supported via duck typing).

``turns_excerpts`` — a chronologically-ordered list of turn entries. Per
the spec, several heuristics are role-sensitive ("first substantial
*user* excerpt", "*assistant* excerpt matches done/completed/...", "if
*user* excerpts show 'actually'/'instead'/'pivot'"), so each entry may
carry role (and optionally timestamp) metadata rather than being a bare
string. Supported shapes, in order of preference:

* An object with ``.role`` / ``.content_excerpt`` / ``.timestamp_ms`` /
  ``.record_type`` / ``.error_text`` attributes (this is exactly the
shape of :class:`scripts.dreamy.protocol.SourceRecord`, so raw
  records can be passed straight through). Error records emitted by the
  connectors (``record_type="error"``) typically carry a blank
  ``content_excerpt`` with the message in ``error_text`` instead — that
  is honored: ``error_text`` is used as the turn's text when
  ``content_excerpt`` is empty, and ``record_type == "error"`` always
  counts as an error for the ``error_burst`` finding regardless of
  whether the text matches the fallback error-word regex.
* A ``dict`` with ``role`` and one of ``content_excerpt`` / ``text`` /
  ``content`` (or ``error_text``), plus optional ``timestamp_ms`` /
  ``ts_ms`` and ``record_type``.
* A ``(role, text)`` or ``(role, text, timestamp_ms)`` tuple.
* A plain ``str`` with no role information.

When *no* entry in the input carries role information (the legacy,
pre-role-tagging shape — every entry a bare ``str``), each role-sensitive
heuristic falls back to treating every excerpt as eligible, matching the
original role-agnostic behaviour. Once role information is present,
"user" heuristics only consider role == "user" and "assistant"
heuristics only consider role == "assistant"; entries whose role is
present but neither ("system", "tool", etc.) are excluded from both.

``pivot_point_ms`` is populated whenever the pivoting turn carries a
timestamp; it stays ``None`` when the input provides no timestamps (e.g.
plain strings or role-only tuples), which is the current default
correlate.py contract.

``git_evidence`` — a :class:`scripts.dreamy.changes.GitEvidence`
instance (or ``None``). Only accessed via ``getattr(..., name, default)``
so any duck-typed object (or ``None``) works without crashing.
"""
from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .changes import docs_drift_signal
from .logging_util import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids any runtime coupling
    from .changes import GitEvidence

log = get_logger("analyze")

# --------------------------------------------------------------------------
# Tunable heuristic constants
# --------------------------------------------------------------------------

_MIN_INTENT_LEN = 40
_ABANDONED_AFTER_MS = 7 * 24 * 3600 * 1000  # 7 days
_ERROR_BURST_THRESHOLD = 3
_NEXT_TASK_SEED_CAP = 10
_DOCS_DRIFT_MEDIUM = 3
_DOCS_DRIFT_HIGH = 10
_FINDING_EVIDENCE_MATCH_CAP = 5

_COMPLETION_RE = re.compile(r"\b(done|completed|fixed|all tests pass)\b", re.IGNORECASE)
_ERROR_RE = re.compile(r"\b(error|exception|traceback|failed|failure)\b", re.IGNORECASE)
_PIVOT_RE = re.compile(r"\b(actually|instead|pivot)\b", re.IGNORECASE)
_TODO_RE = re.compile(r"\b(TODO|FIXME)\b", re.IGNORECASE)
_PENDING_RE = re.compile(r"\b(next|still need|pending|not yet)\b", re.IGNORECASE)

_ROLE_USER = "user"
_ROLE_ASSISTANT = "assistant"


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass
class Finding:
    """A single mechanically-derived observation about a project."""
    category: str
    severity: str
    title: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: str = "medium"
    project_path: str = ""
    # Exactly "deterministic" or "agent", never null — a reader must always be
    # able to tell mechanical fact from model inference (SPEC §5.2 T3).
    provenance: str = "deterministic"


@dataclass
class IntentEpisode:
    """One reconciliation window: what the user seemed to want, and its fate."""
    project_path: str
    started_ms: int
    ended_ms: int
    original_intent: str
    completion_status: str  # complete|in_progress|abandoned|unverified
    completion_evidence: dict[str, Any] = field(default_factory=dict)
    drift_type: str | None = None
    pivot_point_ms: int | None = None


@dataclass
class AnalysisResult:
    episodes: list[IntentEpisode] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    next_task_seeds: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Duck-typed field access — works for dataclasses, dicts, or None
# --------------------------------------------------------------------------

def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# Turn normalization — accepts str / tuple / dict / SourceRecord-like input
# --------------------------------------------------------------------------

@dataclass
class _Turn:
    """A normalized excerpt: role (may be ""), text, timestamp, source
    record_type/error_text (when known), and this turn's position in the
    (post-filtering, non-empty) normalized sequence."""
    role: str
    text: str
    ts_ms: int | None
    idx: int
    record_type: str = ""
    error_text: str = ""


def _coerce_turn_fields(item: Any) -> tuple[str, str, int | None, str, str]:
    """Extract (role, text, ts_ms, record_type, error_text) from one raw
    turns_excerpts entry. record_type/error_text are only populated for
    dict or SourceRecord-like inputs — connectors emit error records
    (record_type="error") with content_excerpt blank and the message in
    error_text instead (see connectors/claude.py, connectors/codex.py,
    connectors/omp_pi.py)."""
    if isinstance(item, str):
        return "", item, None, "", ""

    if isinstance(item, tuple):
        if len(item) == 2:
            role, text = item
            return (role or ""), (text or ""), None, "", ""
        if len(item) >= 3:
            role, text, ts_ms = item[0], item[1], item[2]
            return (role or ""), (text or ""), ts_ms, "", ""
        return "", "", None, "", ""

    if isinstance(item, dict):
        text = item.get("content_excerpt") or item.get("text") or item.get("content") or ""
        role = item.get("role") or ""
        ts_ms = item.get("timestamp_ms", item.get("ts_ms"))
        record_type = item.get("record_type") or ""
        error_text = item.get("error_text") or ""
        return role, text, ts_ms, record_type, error_text

    # Duck-typed object, e.g. protocol.SourceRecord.
    text = getattr(item, "content_excerpt", None)
    if text is None:
        text = getattr(item, "text", "") or ""
    role = getattr(item, "role", "") or ""
    ts_ms = getattr(item, "timestamp_ms", None)
    record_type = getattr(item, "record_type", "") or ""
    error_text = getattr(item, "error_text", "") or ""
    return role, text, ts_ms, record_type, error_text


def _normalize_turns(turns_excerpts: Sequence[Any] | None) -> list[_Turn]:
    """Flatten heterogeneous turn entries into ordered, non-empty _Turn rows.

    When content_excerpt is blank but error_text is present (the shape
    connectors emit for record_type="error" rows), error_text becomes the
    turn's text so error records are never silently dropped."""
    normalized: list[_Turn] = []
    for item in turns_excerpts or []:
        if item is None:
            continue
        role, text, ts_ms, record_type, error_text = _coerce_turn_fields(item)
        text = (text or "").strip()
        error_text = (error_text or "").strip()
        if not text and error_text:
            text = error_text
        if not text:
            continue
        role = (role or "").strip().lower()
        record_type = (record_type or "").strip().lower()
        normalized.append(
            _Turn(
                role=role, text=text, ts_ms=ts_ms, idx=len(normalized),
                record_type=record_type, error_text=error_text,
            )
        )
    return normalized


def _has_role_info(turns: list[_Turn]) -> bool:
    return any(t.role for t in turns)


def _select_role(turns: list[_Turn], role: str, role_known: bool) -> list[_Turn]:
    """Turns matching *role*. Falls back to every turn when the input
    carries no role metadata at all (legacy role-agnostic contract)."""
    if not role_known:
        return turns
    return [t for t in turns if t.role == role]


def _is_error_turn(t: _Turn) -> bool:
    """True for an explicit error record (record_type == "error", the
    connector-emitted signal) or, absent that, text that reads like an
    error (legacy plain-string/regex fallback)."""
    if t.record_type == "error":
        return True
    return bool(_ERROR_RE.search(t.text))


# --------------------------------------------------------------------------
# Heuristic sub-steps
# --------------------------------------------------------------------------

def _session_bounds(sessions: list) -> tuple[int, int]:
    """Return (started_ms, ended_ms) spanning all sessions, or (0, 0)."""
    if not sessions:
        return 0, 0
    starts: list[int] = []
    ends: list[int] = []
    for s in sessions:
        started = _get_field(s, "started_ms", 0) or 0
        ended = _get_field(s, "ended_ms", None)
        if ended is None:
            ended = started
        starts.append(started)
        ends.append(ended)
    return min(starts), max(ends)


def _find_original_intent(user_turns: list[_Turn]) -> tuple[str, int]:
    """First substantial user excerpt (len > _MIN_INTENT_LEN), else '(unknown intent)'.

    Returns (intent_text, idx) where idx is that turn's position in the
    full normalized turn sequence (-1 when nothing matched), so drift
    detection can scan everything strictly after it.
    """
    for t in user_turns:
        if len(t.text) > _MIN_INTENT_LEN:
            return t.text, t.idx
    return "(unknown intent)", -1


def _classify_completion(
    turns: list[_Turn],
    assistant_turns: list[_Turn],
    git_evidence: Any,
    ended_ms: int,
    now_ms: int,
) -> tuple[str, dict[str, Any]]:
    """Deterministic completion classification. Never returns 'complete'.

    A completion-sounding phrase only ever proves *someone said it happened*,
    not that it was verified — so the strongest status this heuristic can
    assign is 'unverified'. Only assistant-role excerpts (or, absent role
    metadata entirely, any excerpt) count as a completion claim; errors are
    counted role-agnostically since they may originate from tool/system
    records as well as the assistant.
    """
    recent_log = _get_field(git_evidence, "recent_log", []) or []
    completion_claim = any(_COMPLETION_RE.search(t.text) for t in assistant_turns)
    error_turns = [t for t in turns if _is_error_turn(t)]

    days_since_last_activity: float | None = None
    # Hardened guard: ended_ms may be None when a session row's ended_ms is
    # NULL or the project lacks any session bounds. Per mission contract,
    # missing bounds must yield unverified/in_progress deterministically
    # and never raise. Treat None identically to 0 (no observable activity).
    bounds_known = bool(ended_ms)
    safe_ended = ended_ms if bounds_known else 0
    safe_now = now_ms if now_ms else 0
    if bounds_known and safe_now:
        days_since_last_activity = (safe_now - safe_ended) / (24 * 3600 * 1000)

    evidence: dict[str, Any] = {
        "completion_claim_matched": completion_claim,
        "recent_commit_count": len(recent_log),
        "error_excerpt_count": len(error_turns),
        "days_since_last_activity": days_since_last_activity,
    }

    if completion_claim and recent_log:
        return "unverified", evidence
    if error_turns:
        return "in_progress", evidence
    if (
        bounds_known
        and not completion_claim
        and safe_now
        and (safe_now - safe_ended) > _ABANDONED_AFTER_MS
    ):
        return "abandoned", evidence
    return "in_progress", evidence


def _detect_drift(
    user_turns: list[_Turn], intent_idx: int
) -> tuple[str | None, int | None]:
    """Look for a pivot marker in user excerpts strictly after the original
    intent excerpt. pivot_point_ms is that turn's timestamp when the input
    supplied one, else None (the common case with untimestamped input)."""
    tail = [t for t in user_turns if t.idx > intent_idx] if intent_idx >= 0 else user_turns
    for t in tail:
        if _PIVOT_RE.search(t.text):
            return "mid_session_pivot", t.ts_ms
    return None, None


def _cite_turns(turns: list[_Turn], prefer_error_text: bool = False) -> list[dict[str, Any]]:
    """Concrete, bounded citations for a finding body.

    A bare count is not evidence — a human reads these at VG-2 and must be able
    to locate the originating turn. Each citation carries the timestamp, the
    source record type and the excerpt itself, capped so one noisy project
    cannot produce an unbounded finding.
    """
    out: list[dict[str, Any]] = []
    for t in turns[:_FINDING_EVIDENCE_MATCH_CAP]:
        text = (t.error_text or t.text) if prefer_error_text else t.text
        out.append({
            "ts_ms": t.ts_ms,
            "record_type": t.record_type or "message",
            "role": t.role or "(unknown)",
            "excerpt": text[:280],
        })
    return out


def _collect_findings(
    project_path: str,
    turns: list[_Turn],
    git_evidence: Any,
    error_excerpt_count: int,
) -> list[Finding]:
    findings: list[Finding] = []

    code_touched = _get_field(git_evidence, "code_paths_touched", []) or []
    docs_touched = _get_field(git_evidence, "docs_paths_touched", []) or []
    if docs_drift_signal(code_touched, docs_touched):
        # Severity scales with surface area: one stray file is noise, a wide
        # code change with zero doc updates is real documentation debt.
        severity = "high" if len(code_touched) >= _DOCS_DRIFT_HIGH else (
            "medium" if len(code_touched) >= _DOCS_DRIFT_MEDIUM else "low"
        )
        findings.append(
            Finding(
                category="docs_drift",
                severity=severity,
                title="Code changed without a matching docs update",
                detail=(
                    f"{len(code_touched)} code path(s) touched, 0 docs path(s) touched"
                ),
                evidence={
                    "code_paths_touched": code_touched[:_FINDING_EVIDENCE_MATCH_CAP],
                    "code_path_count": len(code_touched),
                    "docs_paths_touched": docs_touched,
                },
                confidence="medium",
                project_path=project_path,
            )
        )

    pending_turns = [t for t in turns if _PENDING_RE.search(t.text)]
    if pending_turns:
        findings.append(
            Finding(
                category="incomplete_work",
                severity="medium",
                title="Work described as pending was never confirmed finished",
                detail=(
                    f"{len(pending_turns)} excerpt(s) describe pending or not-yet-done work"
                ),
                evidence={
                    "match_count": len(pending_turns),
                    "citations": _cite_turns(pending_turns),
                },
                confidence="medium",
                project_path=project_path,
            )
        )

    todo_turns = [t for t in turns if _TODO_RE.search(t.text)]
    if todo_turns:
        findings.append(
            Finding(
                category="tech_debt",
                severity="low",
                title="TODO/FIXME markers present in recent activity",
                detail=f"{len(todo_turns)} excerpt(s) contain TODO/FIXME markers",
                evidence={
                    "match_count": len(todo_turns),
                    "citations": _cite_turns(todo_turns),
                },
                confidence="high",
                project_path=project_path,
            )
        )

    if error_excerpt_count >= _ERROR_BURST_THRESHOLD:
        error_turns = [t for t in turns if _is_error_turn(t)]
        findings.append(
            Finding(
                category="error_burst",
                severity="high",
                title="Repeated errors detected in recent activity",
                detail=f"{error_excerpt_count} error record(s)/excerpt(s) found",
                evidence={
                    "error_excerpt_count": error_excerpt_count,
                    "citations": _cite_turns(error_turns, prefer_error_text=True),
                },
                confidence="medium",
                project_path=project_path,
            )
        )

    return findings


def _collect_next_task_seeds(turns: list[_Turn], user_turns: list[_Turn]) -> list[str]:
    """Pending-sounding user lines, plus generic incomplete markers
    (TODO/FIXME) from any role — matches spec: "pending-sounding user
    lines / incomplete markers"."""
    seeds: list[str] = []
    seen: set[str] = set()

    for t in user_turns:
        if t.text not in seen and _PENDING_RE.search(t.text):
            seeds.append(t.text)
            seen.add(t.text)
            if len(seeds) >= _NEXT_TASK_SEED_CAP:
                return seeds

    for t in turns:
        if len(seeds) >= _NEXT_TASK_SEED_CAP:
            break
        if t.text not in seen and _TODO_RE.search(t.text):
            seeds.append(t.text)
            seen.add(t.text)

    return seeds


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------

def analyze_project(
    project_path: str,
    sessions: list,
    turns_excerpts: Sequence[Any] | None,
    git_evidence: GitEvidence | None = None,
) -> AnalysisResult:
    """Run deterministic reconciliation analysis for one project.

    Never raises on empty input: with no sessions and no excerpts this
    still returns a well-formed AnalysisResult (a single episode carrying
    "(unknown intent)" / "in_progress", no findings, no seeds) rather than
    crashing or returning None.

    See the module docstring for the accepted ``turns_excerpts`` entry
    shapes and how role metadata (or its absence) affects each heuristic.
    """
    started_ms, ended_ms = _session_bounds(sessions or [])
    now_ms = _now_ms()

    turns = _normalize_turns(turns_excerpts)
    role_known = _has_role_info(turns)
    user_turns = _select_role(turns, _ROLE_USER, role_known)
    assistant_turns = _select_role(turns, _ROLE_ASSISTANT, role_known)

    original_intent, intent_idx = _find_original_intent(user_turns)
    completion_status, completion_evidence = _classify_completion(
        turns, assistant_turns, git_evidence, ended_ms, now_ms
    )
    drift_type, pivot_point_ms = _detect_drift(user_turns, intent_idx)

    episode = IntentEpisode(
        project_path=project_path,
        started_ms=started_ms,
        ended_ms=ended_ms,
        original_intent=original_intent,
        completion_status=completion_status,
        completion_evidence=completion_evidence,
        drift_type=drift_type,
        pivot_point_ms=pivot_point_ms,
    )

    findings = _collect_findings(
        project_path,
        turns,
        git_evidence,
        completion_evidence["error_excerpt_count"],
    )
    next_task_seeds = _collect_next_task_seeds(turns, user_turns)

    log.info(
        "analyzed project",
        project_path=project_path,
        completion_status=completion_status,
        drift_type=drift_type or "",
        role_known=role_known,
        finding_count=len(findings),
        seed_count=len(next_task_seeds),
    )

    return AnalysisResult(
        episodes=[episode],
        findings=findings,
        next_task_seeds=next_task_seeds,
    )
