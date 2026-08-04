"""Cold-start checker (R6a) — run as a FRESH process over generated artifacts.

The criterion this enforces: a person with no context opens a resumption
prompt, and every path it cites actually exists, and no required section is
empty. One broken citation fails the gate.

Deliberately imports nothing from the rest of dreamy EXCEPT ONE function:
`prompt_compiler.current_compiler_stamp` (D24). Every other check here is
independent of the generator by design — re-running the generator's own
body-rendering logic to confirm the file matches would only prove the
generator agrees with itself. The stamp check is a genuinely different
question: "was this file produced by code that matches what's on disk
RIGHT NOW", the same class of check as verifying a compiled binary matches
its claimed source. Hand-duplicating the hash algorithm here instead of
importing it would risk two copies silently drifting apart — a correctness
bug, not an independence win. See acceptance/VG-4/D24-stale-live-tree.md.

Usage:
    python3 -m dreamy.coldstart <reports_root> [--json]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REQUIRED_RESUMPTION_SECTIONS = [
    "Scope",
    "Source citations",
    "Git state",
    "Completed work",
    "In-progress work",
    "Pending work",
    "Decisions made",
    "Gotchas encountered",
    "Validation commands",
    "Stop conditions",
    "Resume command",
    "/goal condition",
]

# Placeholder bodies that satisfy "section present" while carrying no content.
_EMPTY_MARKERS = ("(none)", "no scope provided", "HEAD unknown", "(not recorded)")

_SECTION_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
# Absolute POSIX paths cited in prose or fenced blocks.
_PATH_RE = re.compile(r"(?<![\w])(/(?:Users|tmp|var|opt|private)/[^\s`'\"<>)\],]+)")
_SKILL_REF_RE = re.compile(r"Skill\(|/skill:|@skill")
# Chains whose expansion ran but produced no body. Emitted by
# prompt_compiler._skill_block() when skill_pins.read_skill_body() returns None.
_MISSING_SKILL_RE = re.compile(r'<skill name="([^"]+)" missing="true"')
# D24: the compiler-version stamp embedded in the footer by
# prompt_compiler.emit_artifact. Absence (pre-v4 file) must count as stale
# too — a footer with no `compiler=` term predates the stamp entirely and
# is exactly the D24 failure mode (a pre-fix file with no way to detect it).
_COMPILER_STAMP_RE = re.compile(r"compiler=([0-9a-f]+)")


def _sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        out[m.group(1).strip()] = text[m.end():end].strip()
    return out


def _artifact_body(text: str) -> str:
    """The generated body, with inlined <skill> documentation removed.

    Skill bodies are third-party reference text quoted verbatim for
    portability; their example paths are illustrations, not claims this
    artifact makes. Checking them would grade the skill author's docs rather
    than the generated prompt.
    """
    return re.sub(r"<skill\b[^>]*>.*?</skill>", "", text, flags=re.DOTALL)

def _artifact_evidence_root(path: Path) -> Path | None:
    """The artifact's own evidence directory, or None if the layout is unexpected.

    Artifacts are emitted at
    `<out>/reports/latest/projects/<slug>/prompts/<artifact_type>.md`, and the
    compiler creates `<out>/reports/latest/projects/<slug>/evidence/`
    (prompt_compiler._ensure_evidence_dir). The evidence root is therefore a
    sibling of `prompts/`, one level ABOVE the artifact's own directory — not a
    sibling of the artifact itself.

    Derived from the artifact's location rather than by scanning its text for a
    literal "evidence" component: this checker must not trust an artifact to
    declare its own workspace.

    Returns None when the artifact is not under a `prompts/` directory, so an
    unrecognized layout exempts nothing and every citation is checked normally.
    """
    if path.parent.name != "prompts":
        return None
    return path.parent.parent / "evidence"

def _is_prerequisite_created(cited: str, evidence_root: Path | None) -> bool:
    """True when `cited` is inside the artifact's own evidence root.

    Such a path is created by the prompt's prerequisites step (R6g), so its
    absence at generation time is expected and not a broken citation.

    Compares resolved paths, so `..` traversal cannot smuggle an outside path in
    under an evidence-root prefix. The root itself counts: the prerequisites step
    creates the directory too.
    """
    if evidence_root is None:
        return False
    try:
        c = Path(cited).resolve()
        r = Path(evidence_root).resolve()
    except (OSError, ValueError):
        return False
    return c == r or r in c.parents


def check_artifact(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    sections = _sections(text)
    body_only = _artifact_body(text)
    kind = path.stem

    result: dict[str, Any] = {
        "path": str(path),
        "artifact_type": kind,
        "sections_found": len(sections),
        "empty_sections": [],
        "missing_sections": [],
        "broken_citations": [],
        # Cited paths the prompt's own prerequisites step will create (R6g).
        # Tracked, not counted as broken — visible so a reviewer can audit the
        # exemption rather than take it on trust.
        "prerequisite_citations": [],
        "checked_citations": 0,
        "unresolved_skill_refs": len(_SKILL_REF_RE.findall(text)),
        # A DIFFERENT failure from `unresolved_skill_refs`. That counter matches
        # unexpanded tokens (`Skill(`, `/skill:`, `@skill`). This one counts
        # chains where expansion RAN but the body was not found, so the compiler
        # emitted `<skill name="X" missing="true"></skill>`. The prompt then
        # declares a skill it never supplies, and the reader is told to follow
        # semantics that are not present. Counting it under the existing metric
        # would conflate two distinct generator faults.
        "missing_skill_bodies": _MISSING_SKILL_RE.findall(text),
        "relative_evidence_refs": 0,
        # D24: True when the footer's `compiler=` stamp does not match what
        # today's compiler would produce for this artifact_type — including
        # a footer with NO `compiler=` term at all (pre-v4 file, the exact
        # D24 failure mode: a file with no way to detect it was stale).
        # `stamp_check_error` is set (and the artifact fails) if the
        # expected stamp itself could not be computed — fail closed, never
        # silently skip the check.
        "stale_compiler_stamp": None,
        "found_compiler_stamp": None,
        "expected_compiler_stamp": None,
        "stamp_check_error": None,
    }

    if kind == "resumption":
        for needed in REQUIRED_RESUMPTION_SECTIONS:
            body = sections.get(needed)
            if body is None:
                result["missing_sections"].append(needed)
            elif not body or body.lower() in _EMPTY_MARKERS or body.strip() in ("- (none)", "(none)"):
                result["empty_sections"].append(needed)

    # R6g: no relative evidence/ path may survive into a generated gate.
    result["relative_evidence_refs"] = len(
        re.findall(r"(?<![\w/.-])evidence/[A-Za-z0-9_./-]+", body_only)
    )

    # Every cited absolute path must exist on disk. A prompt that points at
    # something that is not there is worse than one that points at nothing.
    #
    # Exception — prerequisite-created paths (D1 class b). Per R6g and the
    # addendum, "the evidence directory is created by the prompt's prerequisites
    # step, not assumed to exist." A prompt citing a path that its own
    # prerequisites will create is correct by construction, so flagging it is a
    # checker bug, not a generator bug.
    #
    # The boundary is the artifact's own evidence root. Paths beneath it are the
    # prompt's declared workspace; everything else must exist at generation time.
    # Scoping it to this artifact's root matters — a global "any path containing
    # /evidence/" rule would excuse genuinely broken citations into other
    # projects' trees.
    evidence_root = _artifact_evidence_root(path)
    seen: set[str] = set()
    for m in _PATH_RE.finditer(body_only):
        cited = m.group(1).rstrip(".,;:")
        if cited in seen:
            continue
        seen.add(cited)
        result["checked_citations"] += 1
        if _is_prerequisite_created(cited, evidence_root):
            result["prerequisite_citations"].append(cited)
            continue
        if not Path(cited).exists():
            result["broken_citations"].append(cited)

    stamp_match = _COMPILER_STAMP_RE.search(text)
    found_stamp = stamp_match.group(1) if stamp_match else None
    result["found_compiler_stamp"] = found_stamp
    try:
        expected_stamp = _expected_compiler_stamp(kind)
        result["expected_compiler_stamp"] = expected_stamp
        result["stale_compiler_stamp"] = found_stamp != expected_stamp
    except Exception as exc:  # noqa: BLE001
        result["stamp_check_error"] = str(exc)
        result["stale_compiler_stamp"] = True

    result["ok"] = (
        not result["missing_sections"]
        and not result["empty_sections"]
        and not result["broken_citations"]
        and result["unresolved_skill_refs"] == 0
        and not result["missing_skill_bodies"]
        and result["relative_evidence_refs"] == 0
        and result["stale_compiler_stamp"] is not True
    )
    return result


def _expected_compiler_stamp(artifact_type: str) -> str:
    """What today's compiler WOULD stamp this artifact type with.

    Raises on any failure to import or compute — deliberately does NOT
    degrade to a permissive "cannot verify" state. The acceptance criterion
    is "coldstart must fail on stale stamps"; an inability to compute the
    expected stamp is itself a reason the checker cannot vouch for
    freshness, and must fail closed rather than silently pass artifacts
    through unchecked.
    """
    from . import prompt_compiler

    return prompt_compiler.current_compiler_stamp(artifact_type)


def check_tree(reports_root: Path) -> dict[str, Any]:
    artifacts = sorted(Path(reports_root).rglob("prompts/*.md"))
    per_artifact = [check_artifact(p) for p in artifacts]
    summary = {
        "artifact_count": len(per_artifact),
        "broken_citations": sum(len(a["broken_citations"]) for a in per_artifact),
        # Exempted as prerequisite-created (R6g). Surfaced so the exemption is
        # auditable: a reviewer can see exactly how many citations were waived
        # and confirm the count is not quietly absorbing real breakage.
        "prerequisite_citations": sum(len(a["prerequisite_citations"]) for a in per_artifact),
        "empty_sections": sum(len(a["empty_sections"]) for a in per_artifact),
        "missing_sections": sum(len(a["missing_sections"]) for a in per_artifact),
        "unresolved_skill_refs": sum(a["unresolved_skill_refs"] for a in per_artifact),
        "missing_skill_bodies": sum(len(a["missing_skill_bodies"]) for a in per_artifact),
        "missing_skill_names": sorted({
            n for a in per_artifact for n in a["missing_skill_bodies"]
        }),
        "relative_evidence_refs": sum(a["relative_evidence_refs"] for a in per_artifact),
        "checked_citations": sum(a["checked_citations"] for a in per_artifact),
        # D24: artifacts whose footer stamp does not match today's compiler
        # (or has no stamp at all — pre-v4). This is what would have caught
        # the 17 stale rows on the live tree before they were ever served.
        "stale_compiler_stamps": sum(1 for a in per_artifact if a["stale_compiler_stamp"] is True),
        "stamp_check_errors": [
            {"path": a["path"], "error": a["stamp_check_error"]}
            for a in per_artifact if a["stamp_check_error"]
        ],
        "failing": [a["path"] for a in per_artifact if not a["ok"]],
    }
    summary["verdict"] = "PASS" if (
        summary["artifact_count"] > 0 and not summary["failing"]
    ) else "FAIL"
    summary["artifacts"] = per_artifact
    return summary


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python3 -m dreamy.coldstart <reports_root> [--json]", file=sys.stderr)
        return 2
    root = Path(argv[0]).expanduser()
    summary = check_tree(root)
    if "--json" in argv:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"verdict: {summary['verdict']}")
        print(f"artifacts: {summary['artifact_count']}")
        print(f"citations checked: {summary['checked_citations']}")
        print(f"broken citations: {summary['broken_citations']}")
        print(f"empty sections: {summary['empty_sections']}")
        print(f"missing sections: {summary['missing_sections']}")
        print(f"unresolved skill refs: {summary['unresolved_skill_refs']}")
        print(f"relative evidence refs: {summary['relative_evidence_refs']}")
        print(f"stale compiler stamps: {summary['stale_compiler_stamps']}")
        for failing in summary["failing"][:10]:
            print(f"  FAIL {failing}")
    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
