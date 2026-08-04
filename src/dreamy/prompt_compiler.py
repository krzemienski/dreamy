"""Prompt compiler — R6a (resumption) / R6b (validation) / R6c (remediation)
/ R6d (next_tasks) with R6e (constraint preamble) / R6f (inline emit) /
R6g (absolute evidence paths).

Default skill chains come from addendum Part A. Inline emit mode reads
each skill's SKILL.md and inserts the literal body under `<skill name="...">`
headers — never emits Skill(/skill:/@skill tokens. Byte-identical when
skills pinned and evidence unchanged.
"""
from __future__ import annotations

import hashlib
import re
import warnings
from pathlib import Path
from typing import Any

DEFAULT_CHAINS = {
    "resumption": ["task-architect", "transform-validation-prompt", "goal-condition-architect"],
    "validation": ["functional-validation", "e2e-validate", "transform-validation-prompt"],
    "remediation": ["create-validation-plan", "transform-validation-prompt", "no-mocking-validation-gates"],
    # `ak-research-prompt`, not `akresearch-prompt` — the hyphen was dropped, so
    # resolution never matched and the chain emitted missing="true".
    "next_tasks": ["task-architect", "ak-research-prompt"],
    "_constraints": ["no-mocking-validation-gates", "gate-validation-discipline", "verification-before-completion"],
}


CONSTRAINT_PREAMBLE = """<iron_rule>
IF the real system doesn't work, FIX THE REAL SYSTEM.
NEVER create mocks, stubs, test doubles, or test files.
ALWAYS validate through the same interfaces real users experience.
ALWAYS capture evidence. ALWAYS review evidence. ALWAYS write verdicts.
</iron_rule>

<mock_detection>
"Let me add a mock fallback"  → Fix why the real dependency is unavailable
"I'll write a quick unit test" → Run the real app, look at the real output
"I'll stub this database"      → Start a real database instance
"The real system is too slow"  → That is a real bug. Fix it.
"I'll add a test mode flag"    → There is one mode: production. Test that.
"Just for local development"   → Use the same setup a user would
</mock_detection>

<completion_discipline>
No completion claim without fresh verification evidence in the same message.
Evidence LOCATION is not evidence CONTENT — open it, read it, cite it.
Cite specific proof: file path, line number, exact output string.
</completion_discipline>"""


RESUMPTION_SECTIONS = [
    "scope",
    "source citations",
    "git state",
    "completed work",
    "in-progress work",
    "pending work",
    "decisions made",
    "gotchas encountered",
    "validation commands",
    "stop conditions",
    "resume command",
    "/goal condition",
]


PLATFORM_NAMES = {"ios", "cli", "api", "web", "fullstack", "generic"}


class EmptySectionError(RuntimeError):
    pass


_GOAL_CONCRETE_SIGNAL_RE = re.compile(
    r"`[^`]+`"                             # backtick-fenced command
    r"|(?<![\w])/(?:[\w.\-]+/)+[\w.\-]+"   # a path with >=1 slash-separated segment
    r"|\bexit\s*(?:code)?\s*\d+\b"         # exit-code reference: "exit 0", "exit code 1"
    r"|==|!=|<=|>=|&&|\|\|",               # comparison / shell-control operators
    re.IGNORECASE,
)


def _is_unfalsifiable(goal: str) -> bool:
    """R6a — True when a `/goal` condition names no concrete, checkable signal.

    Per phase-03-compiler-validator-hardening.md's design note: falsifiability
    detection via string heuristics is inherently imperfect, and this check is
    deliberately narrow — it closes the SPECIFIC named gap (the spec's own
    failing example, "the code is correct", must be rejected), not a
    general-purpose natural-language quality classifier. A condition is
    accepted the moment it names any one of: a backtick-fenced command, a
    file path, an exit-code reference, or a comparison/shell-control
    operator. Verified live against both real production `resumption.md`
    `/goal` lines (`~/.local/share/dreamy/reports/latest/projects/*/prompts/
    resumption.md`) — both name a backtick-fenced `echo`/`grep` pipeline and
    pass. The existing default, "echo goal-reached && exit 0", passes on
    both the "&&" operator and the "exit 0" reference.
    """
    return not _GOAL_CONCRETE_SIGNAL_RE.search(goal or "")


_OBSERVABLE_PASS_CRITERIA_SIGNAL_RE = re.compile(
    r"\d|`[^`]+`|\"[^\"]+\"|'[^']+'|\bexit\b",
    re.IGNORECASE,
)


def _has_observable_signal(pass_criteria: str) -> bool:
    """R6b — True when a SHORT `pass_criteria` string still names something checkable.

    Length-first design (adopted from tournament Candidate 2, see phase-03
    plan): a criterion over ~20 characters is accepted outright regardless
    of vocabulary — chosen so the spec's own 9-char failing example "App
    works" fails on length alone, while both real production `pass_criteria`
    strings (well over 200 characters) clear the gate on their own with no
    further check. This signal check exists only to give short strings a
    second chance: a digit, a backtick-fenced or quoted literal, or the word
    "exit" (present in both real production strings and the spec's own R6b
    prose about a gate's `verdict` field) counts. An earlier draft heuristic
    (digit-or-fixed-verb-list) would have falsely rejected both real
    production strings before length-gating was added — do not revert to it.
    """
    return bool(_OBSERVABLE_PASS_CRITERIA_SIGNAL_RE.search(pass_criteria or ""))


def _chains(cfg) -> dict[str, list[str]]:
    override = getattr(cfg, "prompt_chains", None) or {}
    out = {k: list(v) for k, v in DEFAULT_CHAINS.items()}
    for k, v in override.items():
        if isinstance(v, list):
            out[k] = list(v)
    return out


def _resolve_chain(artifact_type: str, cfg) -> list[str]:
    chains = _chains(cfg)
    if artifact_type in ("resumption", "validation", "remediation", "next_tasks"):
        return list(chains.get(artifact_type, []))
    return []


def _resolve_constraints(cfg) -> list[str]:
    return list(_chains(cfg).get("_constraints", []))


def detect_unresolved_skill_refs(content: str) -> list[str]:
    found = []
    for pat in (r"Skill\(", r"/skill:", r"@skill"):
        for m in re.finditer(pat, content):
            found.append(f"{m.group(0)}@{m.start()}")
    return found


def _ensure_evidence_dir(output_dir: Path, project_slug: str) -> Path:
    p = Path(output_dir) / "reports" / "latest" / "projects" / project_slug / "evidence"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _rewrite_evidence_paths(content: str, output_dir: Path, project_slug: str) -> str:
    """Rewrite relative 'evidence/…' paths to absolute under the output dir (R6g).

    Illustrative placeholders are left alone: a token containing `{`, `<`, or
    `*` is a documentation stand-in (``evidence/vg{N}-{desc}.{ext}``), not a
    real citation. Rewriting one manufactures an absolute path that can never
    exist and turns a cold-start check into a false failure.
    """
    abs_root = _ensure_evidence_dir(output_dir, project_slug)
    abs_prefix = str(abs_root)
    pattern = re.compile(r"(?<![\w/])(\.?/?)evidence/(?P<rest>\S*)")

    def repl(match: re.Match[str]) -> str:
        rest = match.group("rest")
        if any(ch in rest for ch in "{}<>*"):
            return match.group(0)
        return f"{abs_prefix}/{rest}"

    return pattern.sub(repl, content)


def _safe_emit_one_skill_block(name: str) -> str:
    """Return a resolved skill block or a bounded missing-skill marker."""

    from . import skill_pins

    body = skill_pins.read_skill_body(name)
    if body is None:
        warnings.warn(
            f"dreamy: skill {name!r} degraded: resolution returned no body",
            RuntimeWarning,
            stacklevel=2,
        )
        return (
            f"## Missing Skill: {name}\n\n"
            "Skill unavailable during compilation; continue using the remaining inlined contracts."
        )
    return f'<skill name="{name}">\n{body}\n</skill>'


def _stable_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _compiler_source_hash() -> str:
    """Hash of the artifact-production code path's source bytes.

    D24 requires the stamp to flip on "any chain/compiler change" — not
    only a change to `DEFAULT_CHAINS`, but any change to how an artifact is
    rendered. D29 proved hashing this module alone is insufficient: the
    renderers live in `evidence_projection.py`, so a sanitizer fix there
    left stamps unchanged, invalidation never fired, and rows kept serving
    pre-fix bytes while looking stamp-fresh. The stamp must cover every
    module whose source can change artifact bytes OR artifact presence:
    the compiler (templates, chains, emit), the projection (evidence
    building, renderers), the analyzer (intent extraction, ranking,
    completion classification), and the compile orchestrator
    (`agents/compiler.py` — its gating decides WHICH artifacts exist).
    """
    h = hashlib.sha256()
    for rel in ("prompt_compiler.py", "evidence_projection.py", "analyze.py", "agents/compiler.py"):
        h.update(rel.encode("utf-8"))
        try:
            h.update((Path(__file__).parent / rel).read_bytes())
        except OSError:
            h.update(b"unknown")
    return h.hexdigest()[:16]


def current_compiler_stamp(artifact_type: str, cfg=None, home: Path | None = None) -> str:
    """Content-derived compiler-version stamp for `prompt_artifacts.compiler_version`.

    D24 (acceptance/VG-4/D24-stale-live-tree.md, "Remedy — supersedes both
    earlier remedies", item 1): a `prompt_artifacts` row carried no record
    of which compiler produced it, so a chain-name fix (D17) could never be
    detected on rows never recompiled — they survived every subsequent run
    untouched and were served to a cold operator as current output.
    `stable_hash` alone cannot catch this: it detects *content* drift, but
    a row that is never recompiled never gets a new hash to compare against.

    Stamp = sha256(artifact_type, resolved chain names in order, per-skill
    body hashes read FRESH from disk via `skill_pins.read_skill_body` —
    reusing the same read path `emit_artifact` itself uses, not the
    persisted `skill_pins` table, so this matches emit_artifact byte for
    byte — and this module's own source hash). Any change to chain
    membership, chain order, a skill body's content, or the compiler's own
    rendering code flips the stamp. `cfg=None` resolves to `DEFAULT_CHAINS`
    with no override, matching the live deployment (no `prompt_chains`
    override is configured).

    Deliberately callable with zero DB/store dependency — `coldstart.py`
    imports only this one narrow function (plus the two read-only inputs
    it wraps: chain resolution and `skill_pins.read_skill_body`) to compute
    what today's compiler WOULD produce, and compares that to the stamp
    embedded in the artifact's own footer. This is not "trusting the
    generator's self-report" — coldstart still runs as a fresh process and
    never reads `prompt_artifacts` or any DB row; it only asks "does this
    file's own footer agree with what current code says it should be."
    """
    class _Empty:
        prompt_chains = None

    eff_cfg = cfg if cfg is not None else _Empty()
    chain_names: list[str] = []
    for _name in _resolve_chain(artifact_type, eff_cfg) + _resolve_constraints(eff_cfg):
        if _name not in chain_names:
            chain_names.append(_name)

    from . import skill_pins as _skill_pins

    # Full digests, in resolved chain order — not truncated, not sorted.
    # Truncating to 8 hex chars trades collision resistance for display
    # brevity that this internal stamp does not need (the footer keeps its
    # own separate 8-char display hashes for humans). Sorting would also
    # hide a chain-ORDER change (e.g. two skills swapped) from the stamp,
    # since a sorted set is order-independent by construction — D24 needs
    # "any chain/compiler change" to flip the stamp, and chain order is
    # part of what the compiler emits (it controls read order in the body).
    skill_hashes: list[str] = []
    for name in chain_names:
        body = _skill_pins.read_skill_body(name, home=home)
        if body is not None:
            skill_hashes.append(name + ":" + hashlib.sha256(body.encode("utf-8")).hexdigest())

    payload = "|".join([
        artifact_type,
        ",".join(chain_names),
        ",".join(skill_hashes),
        _compiler_source_hash(),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _project_slug(project: dict[str, Any], override: str | None = None) -> str:
    if override:
        return override
    return hashlib.sha256((project.get("path") or project.get("id") or "").encode()).hexdigest()[:12]


def _md_section(title: str, body: str) -> str:
    return f"## {title}\n\n{body}\n\n"


def _render_resumption(project: dict[str, Any], evidence: dict[str, Any], cfg) -> str:
    body = []
    body.append(_md_section("Scope", evidence.get("scope", "no scope provided")))
    citations = evidence.get("citations") or []
    body.append(_md_section("Source citations", "\n".join(f"- {c}" for c in citations) or "- (none)"))
    body.append(_md_section("Git state", evidence.get("git_state", "HEAD unknown")))
    body.append(_md_section("Completed work", evidence.get("completed", "(none)")))
    body.append(_md_section("In-progress work", evidence.get("in_progress", "(none)")))
    body.append(_md_section("Pending work", evidence.get("pending", "(none)")))
    body.append(_md_section("Decisions made", evidence.get("decisions", "(none)")))
    body.append(_md_section("Gotchas encountered", evidence.get("gotchas", "(none)")))
    body.append(_md_section("Validation commands", evidence.get("validation_commands", "echo done")))
    body.append(_md_section("Stop conditions", evidence.get("stop_conditions", "all sections non-empty")))
    body.append(_md_section("Resume command", evidence.get("resume_command", "echo resume")))
    goal = evidence.get("goal", "echo goal-reached && exit 0")
    if _is_unfalsifiable(goal):
        raise EmptySectionError(
            "/goal condition is unfalsifiable; it must name a command, path, "
            "exit-code reference, or comparison operator whose output lands "
            "in the transcript"
        )
    body.append(_md_section("/goal condition", goal))
    return "".join(body)


def _render_validation(project: dict[str, Any], evidence: dict[str, Any], cfg) -> str:
    """R6b — one platform reference, six-field gate, gate manifest."""
    platform = evidence.get("platform", "generic")
    evidence_dir = evidence.get("evidence_dir", "")
    body = []
    body.append(_md_section("Scope", evidence.get(
        "scope", "validate completion of unverified work")))
    body.append(_md_section(
        "Detected platform",
        f"`{platform}` — exactly one validation contract is inlined below. "
        f"Loading contracts for platforms that do not apply wastes context and is a "
        f"generation failure.",
    ))
    body.append(_md_section("Gate", evidence.get("gate", "VG-1")))
    body.append(_md_section("Prerequisites", evidence.get("prerequisites", "(none)")))
    body.append(_md_section("Execute", "```sh\n" + str(evidence.get("execute", "")) + "\n```"))
    pass_criteria = evidence.get("pass_criteria", "")
    if len(pass_criteria) < 20 and not _has_observable_signal(pass_criteria):
        raise EmptySectionError(
            "pass_criteria is too vague; it must be observable and specific "
            "('App works' fails generation; a criterion naming a digit, "
            "quoted/backticked literal, or 'exit' passes)"
        )
    body.append(_md_section("Pass criteria", pass_criteria))
    body.append(_md_section("Review", evidence.get("review", "")))
    body.append(_md_section("Verdict", evidence.get("verdict", "")))
    body.append(_md_section(
        "Mock guard",
        "IF tempted to add a mock, stub, test double, fixture, or test-mode flag to make this "
        "gate pass → STOP → fix the real system. A gate satisfied by a substitute proves nothing.",
    ))
    body.append(_md_section(
        "Gate manifest",
        "<gate_manifest>\n"
        "  <total_gates>1</total_gates>\n"
        "  <sequence>VG-1</sequence>\n"
        "  <policy>BLOCKING. No completion claim without a cited evidence line.</policy>\n"
        f"  <evidence_dir>{evidence_dir}</evidence_dir>\n"
        "</gate_manifest>",
    ))
    return "".join(body)


def _render_remediation(project: dict[str, Any], evidence: dict[str, Any], cfg) -> str:
    """R6c — BRIEF → ROADMAP → PLAN → SUMMARY, 2-3 tasks, compounding gates."""
    tasks = evidence.get("tasks") or []
    if len(tasks) > 3:
        raise EmptySectionError("too many tasks; remediation plans must hold 2-3 tasks")
    # R6c: UPPER bound only. A lower-bound check (e.g. `len(tasks) < 2`) is
    # FORBIDDEN here — see phase-03-compiler-validator-hardening.md's "R6c —
    # upper bound ONLY" section. Both real production `remediation.md`
    # artifacts on disk legitimately render exactly 1 task via
    # `evidence_projection.py`'s sentinel/single-episode paths (verified
    # live: a project with zero active remediation work correctly produces
    # a single "nothing to remediate" / "(unknown intent)" roadmap entry).
    # A naive or sentinel-allowlisted lower bound would regress against
    # this real data — a future null-remediation phrasing not on any
    # allowlist would wrongly reject a legitimate artifact. Do not add one.
    body = []
    body.append(_md_section("Brief", evidence.get("brief", "Finish the unfinished work.")))
    body.append(_md_section("Roadmap", "\n".join(f"{i}. {t}" for i, t in enumerate(tasks, 1)) or "- (none)"))
    body.append(_md_section("Plan", evidence.get("plan", "")))
    body.append(_md_section("Summary", evidence.get("summary", "")))
    return "".join(body)


def _render_next_tasks(project: dict[str, Any], evidence: dict[str, Any], cfg) -> str:
    """R6d — ranked, each item citing its motivating evidence and score inputs."""
    items = evidence.get("items") or []
    body = []
    body.append(_md_section(
        "Ranked next tasks",
        "Ranking is explainable: every item shows the inputs that produced its score. "
        "Cost is never an input — cheap work is not better work.",
    ))
    for rank, it in enumerate(items, start=1):
        if "evidence" not in it:
            raise EmptySectionError("next-task item missing evidence citation")
        body.append(f"### {rank}. {it.get('title','(untitled)')}\n\n")
        body.append(f"- score: **{it.get('score','?')}**\n")
        body.append(f"- score inputs: {it.get('score_inputs','(not recorded)')}\n")
        body.append(f"- evidence: {it.get('evidence','')}\n")
        if it.get("blocked_on_unknown"):
            body.append(
                f"- blocked on an unknown — research brief:\n\n"
                f"  > {it.get('research_brief','')}\n"
            )
        body.append("\n")
    if not items:
        body.append("- (none)\n")
    return "".join(body)


def emit_artifact(
    artifact_type: str,
    project: dict[str, Any],
    evidence: dict[str, Any],
    cfg,
    output_dir: Path,
    project_slug: str | None = None,
) -> tuple[str, str, str]:
    """Return (content, stable_hash, footer).

    The footer embeds `compiler=<stamp>` (D24), the content-derived
    compiler-version stamp from `current_compiler_stamp`, so a file-only
    reader (coldstart.py) can recompute and compare it without touching the
    DB. The return signature is UNCHANGED (still a 3-tuple) — the caller
    (`agents/compiler.py`) is out of scope for this fix; `run.py` derives
    and persists `prompt_artifacts.compiler_version` separately, by calling
    `current_compiler_stamp` directly with the same (artifact_type, cfg)
    it already has in its post-loop reconcile pass.

    `cfg.emit_mode` (R6f) is honored: `inline` (default) is unchanged; for
    `native` we additionally emit a bounded machine-readable hint naming
    the resolved skills and a one-shot `RuntimeWarning` — native is
    reserved for a future harness-aware emit, so today the inline
    artifact is still the source of truth and R6f's zero-unresolved-refs
    guarantee is preserved either way.
    """


    output_dir = Path(output_dir)
    slug = _project_slug(project, project_slug)
    # A skill named in both the artifact chain and the shared constraint set
    # (no-mocking-validation-gates is in both) was inlined twice, duplicating
    # kilobytes of identical text in every artifact. Preserve chain order and
    # emit each skill exactly once.
    skill_names: list[str] = []
    for _name in _resolve_chain(artifact_type, cfg) + _resolve_constraints(cfg):
        if _name not in skill_names:
            skill_names.append(_name)
    skill_blocks = []
    skill_hashes = []
    for name in skill_names:
        skill_blocks.append(_safe_emit_one_skill_block(name))
        from . import skill_pins

        body = skill_pins.read_skill_body(name)
        if body is not None:
            skill_hashes.append(name + ":" + _stable_hash(body)[:8])

    chain_label = artifact_type
    used_skill_count = sum(1 for b in skill_blocks if not b.startswith("## Missing Skill:"))
    chain_quality = (
        f"chain: {chain_label} | constraints: {len(_resolve_constraints(cfg))} | "
        f"inlined: {used_skill_count}/{len(skill_names)}"
    )

    if artifact_type == "resumption":
        body = _render_resumption(project, evidence, cfg)
    elif artifact_type == "validation":
        if "/" not in artifact_type:
            pass
        body = _render_validation(project, evidence, cfg)
    elif artifact_type == "remediation":
        body = _render_remediation(project, evidence, cfg)
    elif artifact_type == "next_tasks":
        body = _render_next_tasks(project, evidence, cfg)
    else:
        raise ValueError(f"unknown artifact_type: {artifact_type}")

    # Validate R6a: every required section non-empty.
    if artifact_type == "resumption":
        for needed in RESUMPTION_SECTIONS:
            if needed.lower() not in body.lower():
                raise EmptySectionError(f"resumption missing section: {needed}")

    # R6b: exactly one platform validation contract inlined. Count the declared
    # contract, not incidental word occurrences — the words "api"/"web"/"cli"
    # appear constantly in real prose, so a naive substring scan false-positives
    # on valid output and would push a generator toward vaguer text to appease it.
    if artifact_type == "validation":
        declared = re.findall(r"^## Detected platform$", body, re.MULTILINE)
        if len(declared) != 1:
            raise EmptySectionError(
                "validation must declare exactly one platform contract"
            )
        platform = str(evidence.get("platform", ""))
        if platform not in PLATFORM_NAMES:
            raise EmptySectionError(f"unknown validation platform: {platform!r}")

    # R6g: rewrite evidence paths.
    # Called for its side effect (creates the evidence dir); return unused.
    _ensure_evidence_dir(output_dir, slug)
    body = _rewrite_evidence_paths(body, output_dir, slug)

    # Resolution failures are degraded above. A sigil that survives in the
    # rendered project body is different: content escaped the projection
    # boundary and remains a hard R6f error.
    if detect_unresolved_skill_refs(body):
        raise EmptySectionError("artifact body still contains unresolved skill references")

    # D24 stamp: computed from the SAME chain resolution + skill reads this
    # function just did (not re-derived independently), so it is exactly
    # what this emission would recompute to on a fresh run — that identity
    # is what makes idempotency (second run, 0 recompiles) hold.
    compiler_version = current_compiler_stamp(artifact_type, cfg)

    # Footer (R6 Q12). D24 adds `compiler=<stamp>`, visible in the artifact
    # body itself so coldstart.py — which deliberately imports nothing from
    # the rest of dreamy except this one stamp function — can recompute the
    # expected stamp and flag mismatch without ever touching the DB.
    skills_str = ",".join(skill_hashes[:8]) or "none"
    footer = (
        f"<!-- dreamy-prompt:v1:{_stable_hash(body)[:8]} skills={skills_str} "
        f"compiler={compiler_version} {chain_quality} -->"
    )
    content = (
        f"# {artifact_type.upper()} — {project.get('path','')}\n\n"
        f"{CONSTRAINT_PREAMBLE}\n\n"
        f"## Inlined skill chains\n\n"
        + "\n".join(skill_blocks)
        + "\n\n"
        + body
        + "\n\n"
        + footer
    )

    # R6f: `cfg.emit_mode` selects inline vs native. Inline is the default
    # and produces the same portable artifact as before. Native is reserved
    # for a future harness-aware emit; today we additionally emit a bounded
    # machine-readable hint naming the resolved skills (still no bare
    # Skill(/skill:/@skill tokens, so the unresolved-refs guard below
    # keeps passing) and a one-shot RuntimeWarning so anyone who opted in
    # to native is told plainly that the inline artifact is the source of
    # truth.
    emit_mode = getattr(cfg, "emit_mode", "inline")
    if emit_mode == "native":
        warnings.warn(
            "dreamy: emit_mode='native' is reserved for a future harness-aware "
            "emit; today's artifact is the inline (portable) form with a "
            "native-hint trailer.",
            RuntimeWarning,
            stacklevel=2,
        )
        hint_skills = ",".join(skill_names) or "none"
        content = content + (
            "\n\n## Native emit hint (R6f reserved)\n\n"
            f"<!-- dreamy-native-hint:v1 artifact={artifact_type} "
            f"skills={hint_skills} note=inline-artifact-is-source-of-truth -->\n"
        )

    # R6f: no unresolved Skill(/skill:/@skill tokens. Skill-resolution
    # failures were replaced with bounded markers before assembly; anything
    # left here is a compiler/template leak and remains a hard error.
    if detect_unresolved_skill_refs(content):
        raise EmptySectionError("artifact still contains unresolved skill references")

    stable = _stable_hash(content)
    return content, stable, footer
