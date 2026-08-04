"""Project real run evidence into the per-artifact shapes the compiler renders.

The renderers in :mod:`prompt_compiler` read a flat ``evidence`` dict
(``scope``, ``citations``, ``git_state``, ``completed`` ...). Nothing used to
populate it, so every generated artifact carried literal ``(none)`` sections
and ``HEAD unknown`` — passing the "section exists" check while being useless
to a human, which is exactly the failure R6a's cold-start criterion exists to
catch.

This module is the missing translation layer: deterministic analysis output
(episodes, findings, seeds), read-only git evidence, and correlated session
buckets in; one dict per artifact type out.

Determinism contract (N6): nothing here may embed a run id, a wall-clock
timestamp, or any value that changes between two otherwise-identical runs.
Session/episode timestamps ARE stable — they describe observed history, not
the moment of generation.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC
from pathlib import Path
from typing import Any

# Ephemeral runtime path prefixes that appear in session JSONL excerpts as
# truncated tool-call traces (e.g. "-> read(/tmp/dreamy_...)"). They are
# historical quotes of what a prior agent did, NOT citations of files the
# generated artifact expects the reader to read. The seed/excerpt
# renderers truncate to [:200] / [:160], leaving these as fragments; we
# additionally replace the path substring with a non-actionable token that
# does NOT match the coldstart path regex (no leading '/', no
# '/(Users|tmp|var|opt|private)/' prefix). The surrounding prose --
# including the truncation suffix -- is preserved so the grounding is
# still visible to a human reader as a quoted excerpt of historical work.
# Sanitizer policy: any "/tmp/..." or "/private/tmp/..." substring in prose
# coming from session excerpts is by definition ephemeral (a prior agent's
# scratch dir, a sandbox copy, a verification one-off). Match broadly so
# the corpus can't sneak past it with a new prefix. Real session file paths
# rendered via session_citations() are absolute under ~/.omp, ~/.claude,
# ~/.9router etc. and never flow through this sanitizer.
_EPHEMERAL_PATH_RE = re.compile(
    r"/tmp/[^\s'`\"<>)\],]+"
    r"|/private/tmp/[^\s'`\"<>)\],]+"
)
_EPHEMERAL_TOKEN = "<<ephemeral-runtime-path>>"


def _sanitize_ephemeral_paths(text: str) -> str:
    """Replace ephemeral runtime paths in prose with a non-actionable token.

    Preserves the surrounding text so the grounding is still visible to a
    human reader as a quoted excerpt of historical work, but the path itself
    no longer parses as a filesystem citation. Used on text coming from
    session transcripts that may include truncated agent tool-call traces.
    """
    if not text:
        return text
    return _EPHEMERAL_PATH_RE.sub(_EPHEMERAL_TOKEN, text)


# Session-derived prose regularly QUOTES a skill invocation the user typed
# ("/prewalk /skill:improve-codebase-architecture"). That is historical
# transcript content, not an unexpanded template placeholder — but the bytes
# are indistinguishable, and both R6f (prompt_compiler.py:337) and the
# cold-start portability grep (SPEC-addendum-prompts-p1p2-tui.md §R6f, TASK.md:337) require the
# EMITTED artifact to contain zero of these sigils. Exempting the detector
# would emit an artifact that fails the gate's own grep, so the token must not
# reach the bytes at all.
#
# Defang here, at the same trust boundary as _sanitize_ephemeral_paths: mined
# text in, non-actionable marker out. The compiler's own inlined skill blocks
# never pass through this module, so a genuinely unresolved reference — the
# failure R6f exists to catch — still reaches the detector and still rejects.
_SKILL_REF_RE = re.compile(r"Skill\(|/skill:|@skill")
_QUOTED_SKILL_TOKEN = "<<quoted-skill-ref>>"


def _sanitize_skill_refs(text: str) -> str:
    """Replace quoted skill-invocation sigils with a non-actionable marker.

    The referenced name is preserved, so a human reader still sees which
    skill the historical session invoked; only the sigil that a harness would
    try to resolve is removed.
    """
    if not text:
        return text
    return _SKILL_REF_RE.sub(_QUOTED_SKILL_TOKEN, text)


def _sanitize_projection(obj):
    """Recursively defang mined text across the whole projection payload.

    Applied at the return boundary rather than field-by-field: every string in
    this dict is either derived from session/analysis input or from a static
    contract that contains no sigils, so a blanket pass is safe and cannot be
    forgotten when a new field is added.
    """
    if isinstance(obj, str):
        return _sanitize_skill_refs(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_projection(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_projection(v) for v in obj]
    return obj

# Platform detection, e2e-validate 6-priority table. Exactly one wins.
_PLATFORM_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ios", ("Package.swift", "Podfile", "Info.plist")),
    ("web", ("next.config.js", "next.config.ts", "vite.config.ts", "vite.config.js", "index.html")),
    ("api", ("openapi.yaml", "openapi.json", "docker-compose.yml")),
    ("cli", ("pyproject.toml", "setup.py", "Cargo.toml", "go.mod")),
)

_PLATFORM_VALIDATION: dict[str, dict[str, str]] = {
    # `<scheme>` is unknown at generation time, so the build command cannot run
    # as emitted. Discovery first: `xcodebuild -list` prints the real schemes.
    "ios": {
        "execute": "xcodebuild -list 2>/dev/null || ls -a",
        "pass": (
            "the scheme list is printed, AND you quote the exact scheme name from it, AND "
            "you then run the real build with that scheme. Build succeeds AND the app "
            "launches in the simulator AND a screenshot shows the changed screen rendering "
            "its expected content"
        ),
    },
    "web": {
        "execute": "npm run build && npm run dev",
        "pass": (
            "build exits 0 AND the dev server serves the changed route AND the "
            "browser console reports zero errors on load"
        ),
    },
    # `api` and `cli` cannot know the port, package, or endpoint at generation
    # time. Emitting `curl .../<port>/<changed-endpoint>` was wrong twice over:
    # bare `<...>` is a shell redirection metacharacter (syntax error), and even
    # quoted it resolves to a literal `<port>` URL that cannot succeed. Quoting
    # fixes only the parser, not usability.
    #
    # So these emit a DISCOVERY command that runs as-is and prints real output,
    # and the criterion requires citing that output before running the real
    # invocation. Same shape as `generic`: false until the reader actually reads.
    # The criterion asks for a port AND a route, so the command must surface
    # both. An earlier version grepped only `listen|PORT`, which structurally
    # cannot emit a route line — an UNSATISFIABLE criterion (the inverse of the
    # tautology, equally broken). Two labelled sections, one per fact.
    "api": {
        "execute": (
            "echo '== PORT ==' && "
            "{ grep -rEn 'listen|PORT|port:' --include='*.env*' --include='*.y*ml' "
            "--include='*.json' --include='*.toml' . 2>/dev/null | head -10 || true; } && "
            "echo '== ROUTES ==' && "
            "{ grep -rEn '\"/[a-zA-Z0-9_-]+|@(app|router)\\.(get|post|put|patch|delete)|"
            "(app|router)\\.(get|post|put|patch|delete)\\(' "
            "--include='*.py' --include='*.ts' --include='*.js' --include='*.go' "
            "--include='*.y*ml' --include='*.json' . 2>/dev/null | head -15 || true; }"
        ),
        "pass": (
            "both sections are printed, AND the port and route are each established by "
            "exactly one of these two routes — (a) quote the exact line from '== PORT ==' "
            "or '== ROUTES ==' that names it, or (b) if that section is empty, cite the "
            "specific source that supplies the value instead (framework default, env var "
            "and its default, compose/K8s manifest, or an in-process test client) — AND "
            "you then issue the real request using the established values and show its "
            "status line and body. HTTP status is 2xx AND the body contains the fields "
            "the change introduced, with non-placeholder values. An unsourced value, or a "
            "request you did not actually run, does not satisfy this criterion"
        ),
    },
    "cli": {
        "execute": (
            "ls -a && { grep -m1 -E 'name|scripts|\\[project\\]|module' pyproject.toml "
            "package.json setup.py Cargo.toml go.mod 2>/dev/null || true; }"
        ),
        "pass": (
            "the output is printed, AND you quote the exact line naming the package or "
            "binary, AND you then run its real `--help` plus the changed subcommand. Both "
            "invocations exit 0 AND stdout contains the changed subcommand's real output, "
            "not a usage banner"
        ),
    },
    # `generic` = no platform marker matched, so no entrypoint is known.
    #
    # Two earlier attempts were both wrong:
    #   1. prose ("run the project's own entrypoint...") — not shell-valid; it
    #      is concatenated after `cd <path> &&`, so pasting it fails.
    #   2. `:` no-op — shell-valid, but `:` always exits 0 and prints nothing,
    #      making the R6a goal ("exits 0 AND stdout shows...") a tautology:
    #      auto-satisfied and unfalsifiable.
    #
    # A real discovery command fixes both: it is shell-valid, emits genuine
    # stdout into the transcript, and can fail.
    #
    # The pass criterion must also be falsifiable. An earlier version ended
    # "...or say so explicitly", which made BOTH branches pass — a tautology in
    # the same class as the `:` no-op. It now requires a CITATION back into the
    # command's own stdout, so it is false until the reader actually reads the
    # listing, and wrong if they cite a file the listing does not contain.
    "generic": {
        "execute": "ls -a && cat README* 2>/dev/null | head -40",
        "pass": (
            "the listing is printed, AND you quote the exact filename from that listing "
            "(or the exact README line) that identifies how this project is run, AND you "
            "state the resulting command. If the listing contains no such file, quote the "
            "listing and state that no entrypoint is discoverable — a claim with no quoted "
            "line from the output above does not satisfy this criterion"
        ),
    },
}


def detect_platform(project_path: str) -> str:
    """Return exactly one platform id. Never returns a list (R6b context guard)."""
    root = Path(project_path)
    try:
        if not root.is_dir():
            return "generic"
        names = {p.name for p in root.iterdir()}
    except OSError:
        return "generic"
    for platform, markers in _PLATFORM_MARKERS:
        if any(m in names for m in markers):
            return platform
    return "generic"


def _fmt_ms(ms: int | None) -> str:
    """Stable UTC rendering of an observed timestamp."""
    if not ms:
        return "unknown"
    from datetime import datetime

    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bullets(items: Sequence[str], empty: str) -> str:
    lines = [f"- {i}" for i in items if str(i).strip()]
    return "\n".join(lines) if lines else f"- {empty}"


def session_citations(sessions: Sequence[dict[str, Any]]) -> list[str]:
    """Source citations: session id + harness + local path + observed span.

    D29: `raw_path` is the session transcript's OWN recorded file location,
    not user-quoted prose — but on this machine that location is itself
    under `/private/tmp/claude-501/...` for some harnesses (an ephemeral
    per-process scratch dir), so it is exactly as liable to have vanished
    by read time as any other ephemeral path this module sanitizes.
    """
    out: list[str] = []
    for s in sorted(sessions, key=lambda b: (b.get("started_ms") or 0, str(b.get("native_id"))))[:12]:
        native = s.get("native_id") or "(no native id)"
        path = _sanitize_ephemeral_paths(s.get("raw_path") or "(no source file)")
        out.append(
            f"`{s.get('source_id','?')}` session `{native}` — {path} "
            f"({_fmt_ms(s.get('started_ms'))} → {_fmt_ms(s.get('ended_ms'))}, "
            f"{s.get('turn_count', 0)} turns)"
        )
    return out


def git_state_block(git: dict[str, Any], evidence_dir: Path | None = None) -> str:
    """Human-readable git state at last touch. Never fabricates a HEAD.

    Per-entry paths in the status porcelain are NOT inlined — they are
    potentially non-existent on the reader's machine (the project tree may
    have been wiped, and a status file mentions every file the prior tree
    held). Instead we materialize the raw status to
    `<evidence_dir>/git_status.txt` when an evidence_dir is provided, and
    the prompt body cites only that file (which the renderer guarantees
    exists) plus a bounded summary.
    """
    if not git:
        return "No git evidence: this path is not a git repository, or git could not read it."
    log = git.get("recent_log") or []
    status = (git.get("status_porcelain") or "").strip()
    lines: list[str] = []
    if log:
        head = log[0]
        lines.append(
            f"HEAD {head.get('sha','')[:12]} — {head.get('subject','')} "
            f"({head.get('author','')}, {head.get('date','')})"
        )
        lines.append(f"{len(log)} commit(s) in the observed window.")
    else:
        lines.append("No commits in the observed window.")
    entries: list[str] = []
    if status:
        entries = [ln for ln in status.splitlines() if ln.strip()]
        # Bounded summary, NOT per-entry paths.
        lines.append(f"Working tree DIRTY — {len(entries)} uncommitted entr(ies).")
        # Categorize by status code without revealing paths.
        by_code: dict[str, int] = {}
        for ln in entries:
            code = ln[:2] if len(ln) >= 2 else ln[:1]
            by_code[code] = by_code.get(code, 0) + 1
        for code in sorted(by_code):
            lines.append(f"    {code}: {by_code[code]}")
        # Persist raw status to evidence_dir/git_status.txt so the reader can
        # read every path; the prompt body does not enumerate them.
        if evidence_dir is not None:
            try:
                Path(evidence_dir).mkdir(parents=True, exist_ok=True)
                status_file = Path(evidence_dir) / "git_status.txt"
                status_file.write_text(
                    f"# Raw git status_porcelain for {git.get('project_path','')}\n"
                    f"# entries: {len(entries)}\n\n"
                    + status,
                    encoding="utf-8",
                )
                lines.append(f"Full per-entry status: {status_file}")
            except OSError:
                pass
    else:
        lines.append("Working tree clean.")
    # Distinct from the `code` above, which is a two-char git status prefix.
    code_paths = git.get("code_paths_touched") or []
    docs_paths = git.get("docs_paths_touched") or []
    lines.append(f"Touched: {len(code_paths)} code path(s), {len(docs_paths)} docs path(s).")
    return "\n".join(lines)


def _finding_line(f: dict[str, Any]) -> str:
    prov = f.get("provenance") or "deterministic"
    marker = "(A)" if prov == "agent" else "(D)"
    return (
        f"{marker} [{f.get('severity','medium')}/{f.get('category','debt')}] "
        f"{f.get('title','')} — {f.get('detail','')}"
    )


def _clip(text: str, limit: int) -> str:
    """Clip to `limit` without leaving a severed path or URL behind.

    A blind slice produced citations the cold-start gate reported as broken
    files: `/home/<op>/D` and `/home/<op>/Desktop/some-hyphenated-na` are not
    paths anyone wrote, they are the left half of one. Four such fragments
    failed R6a across three artifacts.

    Truncation itself is correct — these are session excerpts and bounding
    them is the point. What is wrong is leaving behind something that reads
    as addressable, because neither a human nor a checker can distinguish a
    clipped prefix from a genuinely dead path.

    So a final token that looks addressable and did NOT survive the cut
    whole is dropped rather than shortened. A first attempt backed off only
    when the token began past two-thirds of the budget; that threshold let
    `…/yt-transition-` through, because the token starts early and runs
    long. Any partial is now removed: a citation that loses its trailing
    path still carries its prose, whereas a half-path actively misleads.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut_token_start = head.rfind(" ") + 1
    severed = head[cut_token_start:]
    # Severed only if the source continues this token past the cut.
    continues = limit < len(text) and not text[limit].isspace()
    if continues and ("/" in severed or "://" in severed):
        head = head[:cut_token_start]
    return head.rstrip() + "…"


def _evidence_citations(f: dict[str, Any]) -> list[str]:
    cites = (f.get("evidence") or {}).get("citations") or []
    out = []
    for c in cites[:3]:
        out.append(
            f"{_fmt_ms(c.get('ts_ms'))} {c.get('role','?')}: "
            f"{_clip(_sanitize_ephemeral_paths(str(c.get('excerpt', ''))), 160)}"
        )
    return out


def _intent(ep: dict[str, Any], limit: int | None = None) -> str:
    """Sanitized `original_intent` read for every render call site.

    D29: `original_intent` is raw first-user-turn text (analyze.py
    `_find_original_intent`) and was rendered directly at 6+ call sites
    with zero sanitization — including embedded `<task-notification>` /
    `<output-file>` XML blocks and truncated ephemeral tool-output paths
    like `/private/tmp/claude-501/...` that a user's turn can quote
    verbatim from a prior agent's tool trace. `_sanitize_ephemeral_paths`
    existed but only covered `seeds`/`citations`, not this field.

    Centralizing here (one function, one trust boundary) instead of
    patching each render site independently means a future new caller
    of `ep.get('original_intent')` in this module inherits the fix by
    using `_intent(ep)` rather than needing to remember to sanitize.
    """
    raw = ep.get("original_intent") or ""
    clean = _sanitize_ephemeral_paths(raw)
    # Truncation goes through `_clip`, not a bare slice. A blind
    # `clean[:limit]` severed a path mid-token and shipped
    # `/home/<op>/Desktop/dre` into an artifact, where the cold-start gate
    # correctly read it as a citation to a file that does not exist. Same
    # defect class as the citation excerpts, different call site — which is
    # exactly why this function exists as one trust boundary.
    return _clip(clean, limit) if limit is not None else clean

def build(
    project: dict[str, Any],
    analysis_evidence: dict[str, Any],
    output_dir: Path,
    project_slug: str,
) -> dict[str, Any]:
    """Return the flat evidence dict every renderer reads."""
    project_path = project.get("path", "")
    episodes: list[dict[str, Any]] = list(analysis_evidence.get("episodes") or [])
    findings: list[dict[str, Any]] = list(analysis_evidence.get("findings") or [])
    sessions: list[dict[str, Any]] = list(analysis_evidence.get("sessions") or [])
    git: dict[str, Any] = dict(analysis_evidence.get("git") or {})
    seeds: list[str] = list(analysis_evidence.get("next_task_seeds") or [])

    platform = detect_platform(project_path)
    contract = _PLATFORM_VALIDATION[platform]
    evidence_dir = Path(output_dir) / "reports" / "latest" / "projects" / project_slug / "evidence"

    # Detect project availability: a recorded path may not exist on disk
    # (deleted/moved/ephemeral). When unavailable, the prompt body must NOT
    # command the reader to `cd` into the missing path or cite it as an
    # actionable capture target; absolute paths in the body either resolve
    # (evidence_dir exists by render-time mkdir) or are replaced with a
    # bounded non-actionable marker.
    project_path_available = bool(project_path) and Path(project_path).exists()
    unavailable_marker = "<<project-path-not-on-this-machine>>"
    safe_project_path = project_path if project_path_available else unavailable_marker

    by_status: dict[str, list[dict[str, Any]]] = {}
    for ep in episodes:
        by_status.setdefault(ep.get("completion_status") or "in_progress", []).append(ep)

    span_start = min((s.get("started_ms") or 0 for s in sessions), default=0)
    span_end = max((s.get("ended_ms") or 0 for s in sessions), default=0)

    if project_path_available:
        scope_header = f"Resume work on `{project_path}` (detected platform: {platform})."
    else:
        scope_header = (
            f"Project path `{project_path}` is not present on this machine; "
            f"the artifact body uses a bounded unavailable marker `{unavailable_marker}` "
            f"in place of any `cd`/capture-target that would otherwise fail. "
            f"Recorded detected platform: {platform}."
        )
    scope = (
        f"{scope_header}\n\n"
        f"Observed window: {_fmt_ms(span_start)} → {_fmt_ms(span_end)} across "
        f"{len(sessions)} correlated session(s) from "
        f"{len(sorted({s.get('source_id','') for s in sessions}))} harness(es), "
        f"{len(episodes)} intent episode(s), {len(findings)} deterministic finding(s)."
    )

    completed = _bullets(
        [f"{_intent(ep)} — last activity {_fmt_ms(ep.get('ended_ms'))}"
         for ep in by_status.get("unverified", [])],
        "Nothing is confirmed complete. Deterministic analysis never asserts `complete`; "
        "the strongest verdict it issues is `unverified`.",
    )
    in_progress = _bullets(
        [f"{_intent(ep)} — started {_fmt_ms(ep.get('started_ms'))}, "
         f"last activity {_fmt_ms(ep.get('ended_ms'))}"
         for ep in by_status.get("in_progress", [])],
        "No episode is currently in progress.",
    )
    pending_items = [f"{_finding_line(f)}" for f in findings
                     if f.get("category") in ("incomplete_work", "tech_debt", "docs_drift")]
    pending_items.extend(_clip(_sanitize_ephemeral_paths(s), 200) for s in seeds[:5])
    pending = _bullets(pending_items, "No pending work was detected in the observed window.")

    decisions = _bullets(
        [f"Pivot detected at {_fmt_ms(ep.get('pivot_point_ms'))}: intent drifted "
         f"({ep.get('drift_type')}) away from \u201c{_intent(ep, 120)}\u201d"
         for ep in episodes if ep.get("drift_type")],
        "No mid-session pivot was detected; the original intent was never revised.",
    )

    gotcha_lines: list[str] = []
    for f in findings:
        if f.get("category") in ("error_burst", "docs_drift"):
            gotcha_lines.append(_finding_line(f))
            gotcha_lines.extend(f"    {c}" for c in _evidence_citations(f))
    for ep in by_status.get("abandoned", []):
        gotcha_lines.append(
            f"(D) Abandoned without completion evidence: \u201c{_intent(ep, 120)}\u201d "
            f"(last activity {_fmt_ms(ep.get('ended_ms'))})"
        )
    gotchas = _bullets(gotcha_lines, "No error bursts or documentation drift were observed.")

    citations = session_citations(sessions)

    validation_commands = (
        f"# platform: {platform}\n{contract['execute']}\n\n"
        f"# capture evidence under (this directory is created by the prerequisites step):\n"
        f"# {evidence_dir}"
    )
    stop_conditions = (
        "STOP and report instead of continuing if: the validation command cannot run at all; "
        "the observed behaviour contradicts this document; or completing the work would require "
        "writing to a path outside the project."
    )
    # resume_command: when the project path is unavailable on this machine,
    # omit `cd <project>` (would fail) and pair the contract execute with a
    # no-op cwd so the reader gets the same diagnostic shape without an
    # invalid cd.
    if project_path_available:
        resume_command = f"cd {project_path or '.'} && {contract['execute'].splitlines()[0]}"
    else:
        resume_command = (
            f"{contract['execute'].splitlines()[0]}  # cwd: {unavailable_marker}"
        )

    # R6a: the /goal condition must name a command whose output lands in the
    # transcript. A condition about internal correctness can never be proven.
    goal = (
        f"Running `{contract['execute'].splitlines()[0]}` in `{safe_project_path}` prints its "
        f"result into the transcript, and that output satisfies: {contract['pass']}"
    )
    # Also destroyed by the corruption. `git_state_block` is called WITHOUT an
    # evidence_dir: passing one would make it materialize git_status.txt and
    # cite that path, and no caller in the shipping tree does so.
    git_state = git_state_block(git)

    ranked = _rank_next_tasks(findings, episodes, seeds, span_end)

    # R6c remediation inputs. Destroyed by the same corruption that broke the
    # phases loop and the return dict; recovered from the artifact contract.
    #
    # Sources are episodes that stopped WITHOUT completion evidence — the work
    # a remediation plan exists to finish. `unverified` is excluded: it is the
    # strongest verdict deterministic analysis issues, so those episodes are
    # awaiting proof, not abandoned.
    remediation_sources = (
        by_status.get("in_progress", []) + by_status.get("abandoned", [])
    )

    # _render_remediation raises EmptySectionError above 3 tasks
    # (prompt_compiler.py:210), so the cap is a contract, not a preference.
    # One task per unfinished episode, newest first: the most recent stall is
    # the one with live context still worth resuming.
    tasks = [
        _intent(ep, 160) or "Unnamed work item"
        for ep in sorted(
            remediation_sources,
            key=lambda e: e.get("ended_ms") or e.get("started_ms") or 0,
            reverse=True,
        )
    ][:3]
    if not tasks:
        # A remediation artifact still has to render. Naming the absence beats
        # an empty section, which the cold-start checker flags.
        tasks = ["No unfinished episode was observed in this window; nothing to remediate."]

    phases: list[str] = []
    for idx, task in enumerate(tasks, start=1):
        prereq = (
            "None — this is the first phase."
            if idx == 1
            else f"Phase {idx - 1} gate PASSED, re-verified in this session (evidence compounds)."
        )
        phases.append(
            f"### Phase {idx} — {task}\n\n"
            f"<validation_gate id=\"P{idx}\" blocking=\"true\">\n"
            f"  <prerequisites>{prereq}</prerequisites>\n"
            f"  <execute>{contract['execute']}</execute>\n"
            f"  <pass_criteria>{contract['pass']}</pass_criteria>\n"
            f"  <review>Open the captured evidence under {evidence_dir} and read its CONTENT; "
            f"a path alone is not proof.</review>\n"
            f"  <verdict>PASS → Phase {idx + 1} | FAIL → fix the real system, re-run this phase</verdict>\n"
            f"  <mock_guard>IF tempted to stub, mock, or add a test-mode flag to make this pass → STOP → "
            f"fix the real system.</mock_guard>\n"
            f"</validation_gate>"
        )

    return _sanitize_projection({
        # R6a
        "scope": scope,
        "citations": citations,
        "git_state": git_state,
        "completed": completed,
        "in_progress": in_progress,
        "pending": pending,
        "decisions": decisions,
        "gotchas": gotchas,
        "validation_commands": validation_commands,
        "stop_conditions": stop_conditions,
        "resume_command": resume_command,
        "goal": goal,
        # R6b
        "platform": platform,
        "gate": f"VG-1 — prove the work claimed done on `{project_path}` actually runs.",
        "prerequisites": (
            f"mkdir -p {evidence_dir}\n"
            f"Confirm the working tree state recorded below still matches reality."
        ),
        "execute": contract["execute"],
        "pass_criteria": contract["pass"],
        "review": f"Read the captured output under {evidence_dir} and cite the exact line that proves the criterion.",
        "verdict": "PASS only with a cited evidence line. Anything else is FAIL.",
        "evidence_dir": str(evidence_dir),
        # R6c
        "brief": (
            f"{len(remediation_sources)} episode(s) on `{project_path}` stopped without completion "
            f"evidence. This plan finishes them. It is a PROPOSAL — nothing here is applied "
            f"automatically, and no file in the project is modified by dreamy."
        ),
        "tasks": tasks,
        "plan": "\n\n".join(phases),
        "summary": (
            f"{len(tasks)} task(s), each behind a blocking gate. Phase N's prerequisites include "
            f"proof that Phase N−1 still passes, so a regression fails at the boundary rather than "
            f"silently. Output is a proposal under {output_dir}; the repository is never written to."
        ),
        # R6d
        "items": ranked,
    })


def _rank_next_tasks(
    findings: Sequence[dict[str, Any]],
    episodes: Sequence[dict[str, Any]],
    seeds: Sequence[str],
    span_end_ms: int,
) -> list[dict[str, Any]]:
    """Rank candidate next tasks with the score inputs kept visible (R6d)."""
    severity_weight = {"high": 3.0, "medium": 2.0, "low": 1.0}
    items: list[dict[str, Any]] = []

    for f in findings:
        sev = severity_weight.get(f.get("severity", "medium"), 1.0)
        surface = float(len((f.get("evidence") or {}).get("citations") or []))
        score = sev * 2.0 + min(surface, 5.0)
        cites = _evidence_citations(f)
        items.append({
            "title": f.get("title", "(untitled)"),
            "score": round(score, 2),
            "score_inputs": f"severity={f.get('severity')}({sev}) × 2 + evidence_surface={surface}",
            "evidence": "; ".join(cites) if cites else f"{f.get('category')}: {f.get('detail','')}",
            "blocked_on_unknown": False,
        })

    for ep in episodes:
        if ep.get("completion_status") not in ("in_progress", "abandoned"):
            continue
        age_days = max(0.0, (span_end_ms - (ep.get("ended_ms") or span_end_ms)) / 86_400_000)
        drift_bonus = 2.0 if ep.get("drift_type") else 0.0
        score = 4.0 + drift_bonus - min(age_days * 0.1, 2.0)
        intent = _intent(ep).strip()
        # An intent we could not reconstruct is an unknown, not a guess: emit a
        # research brief instead of inventing a task (R6d acceptance).
        unknown = intent in ("", "(unknown intent)")
        items.append({
            "title": (
                "Reconstruct the original intent before resuming"
                if unknown else f"Finish: {_clip(intent, 140)}"
            ),
            "score": round(score, 2),
            "score_inputs": (
                f"unfinished_intent=4.0 + drift={drift_bonus} − age_penalty="
                f"{round(min(age_days * 0.1, 2.0), 2)} ({round(age_days, 1)}d since last activity)"
            ),
            "evidence": (
                f"episode {ep.get('completion_status')} spanning "
                f"{_fmt_ms(ep.get('started_ms'))} → {_fmt_ms(ep.get('ended_ms'))}"
            ),
            "blocked_on_unknown": unknown,
            "research_brief": (
                "Read the cited sessions for this project end to end and determine what the "
                "original objective was, what was already built toward it, and which of that "
                "work survives in the current tree; the ingested excerpts did not contain a "
                "substantial enough opening request to reconstruct the intent mechanically. "
                "Produce a one-paragraph statement of the objective, grounded in cited turns."
            ) if unknown else "",
        })

    for seed in list(seeds)[:3]:
        items.append({
            "title": f"Follow up: {_clip(seed, 140)}",
            "score": 1.5,
            "score_inputs": "pending_marker=1.5 (mentioned as pending, never confirmed done)",
            "evidence": f"session excerpt: {_clip(_sanitize_ephemeral_paths(seed), 200)}",
            "blocked_on_unknown": False,
        })

    items.sort(key=lambda i: (-i["score"], i["title"]))
    return items[:10]
