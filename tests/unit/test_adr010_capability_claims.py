"""ADR-010's wired/unwired table must track the code [SDK-002, SDK-003].

ADR-010 asserts which of the ten `required` SDK capabilities are actually used
in `src/dreamy/`. That table is the countable basis for G3 and G6 staying
BLOCKED, so a stale table would understate or overstate readiness.

This parses the ADR's own table rather than hard-coding a second copy of it: if
someone wires a capability and updates the ADR, this passes; if they wire one
and forget, or claim one that isn't there, it fails naming the row.

The measurement is provenance-scoped, and that is the whole point. The first
version of this file concatenated raw file text and grepped it, which made the
guard a phantom gate -- it passed 12/12 while the count it defended was wrong.
Row 13 (`skills` / `setting_sources`) matches across seven files under
`src/dreamy/`, none of them an SDK option: an argparse variable and its
subcommand plumbing (`cli.py`), a comment describing an artifact footer
(`efficacy.py`), the f-string that emits that footer (`prompt_compiler.py`),
lockfile schema keys (`skills_lock.py`), Dreamy-local skill config and pinning
(`config.py`, `skill_pins.py`), and the generated pin manifest
(`resources/skills.lock`). That last one is why the inventory scan below walks
every suffix rather than `*.py`: the ADR sentence covers `src/dreamy/`
unrestricted, and a `.py`-only guard could not see the artifact it named.
`setting_sources` appears nowhere in `src/dreamy/` -- it does occur elsewhere
in the repository, including in this file's own probe table below, but nowhere
in product code.

Stripping comments and strings via AST was not sufficient either: a bare
`keyword.arg == "skills"` still matches `anything(skills=...)`, so an unrelated
local call could re-mark the row. A capability counts as wired only when the
symbol is traceable to `claude_agent_sdk` -- imported from it, passed as a
keyword to something imported from it, or set as an attribute on such an
object. Measured that way the SDK surface is three imports
(`query`, `ClaudeAgentOptions`, `ResultMessage`) and nine other members. That
count is asserted below, not merely stated: an unpinned number in a docstring
is the same phantom-gate shape this file exists to prevent.
"""

from __future__ import annotations

import ast
import itertools
import pathlib
import re

import pytest

ADR = pathlib.Path(__file__).resolve().parents[2] / "docs/decisions/ADR-010-SDK-CAPABILITY-DISPOSITION.md"
SRC = pathlib.Path(__file__).resolve().parents[2] / "src/dreamy"
SDK_MODULE = "claude_agent_sdk"

# Row number -> symbols that constitute "wired" WHEN traceable to the SDK.
# A row is wired when ANY of its probes appears in the SDK surface.
PROBES: dict[str, tuple[str, ...]] = {
    "1": ("query", "ClaudeSDKClient"),
    "2": ("permission_mode", "can_use_tool"),
    "5": ("hooks", "HookMatcher"),
    "7": ("output_format", "structured_output"),
    "8": ("system_prompt",),
    "9": ("create_sdk_mcp_server", "mcp_servers"),
    "10": ("SdkMcpTool",),
    "11": ("agents", "AgentDefinition"),
    "13": ("setting_sources", "skills"),
    "14": ("total_cost_usd", "max_budget_usd"),
}


def _sdk_surface() -> set[str]:
    """Symbols provably reaching the Agent SDK.

    Three sources, all requiring provenance:
      - names imported from `claude_agent_sdk`
      - keyword arguments passed to a call on such a name
      - attributes read or set on a value produced by such a call

    An `x = foo(skills=...)` where `foo` is local contributes nothing.
    """
    surface: set[str] = set()
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - caught by lint, not here
            continue

        local: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and SDK_MODULE in node.module:
                for alias in node.names:
                    local.add(alias.asname or alias.name)
                    surface.add(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if SDK_MODULE in alias.name:
                        local.add(alias.asname or alias.name)

        # Names bound to the result of an SDK call inherit its provenance.
        derived: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                fn = node.value.func
                if isinstance(fn, ast.Name) and fn.id in local:
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            derived.add(tgt.id)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                base = None
                if isinstance(fn, ast.Name):
                    base = fn.id
                elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                    base = fn.value.id
                if base in local or base in derived:
                    for kw in node.keywords:
                        if kw.arg:
                            surface.add(kw.arg)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in local or node.value.id in derived:
                    surface.add(node.attr)
    return surface


def _source() -> set[str]:
    return _sdk_surface()


def _adr_table() -> dict[str, bool]:
    """Parse the implementation-status table. Returns row -> claimed-wired."""
    rows: dict[str, bool] = {}
    for line in ADR.read_text().splitlines():
        m = re.match(r"^\|\s*(\d+)\s*\|([^|]*)\|([^|]*)\|", line)
        if not m:
            continue
        num, _cap, wired = m.group(1), m.group(2), m.group(3).strip()
        if wired.lower().startswith("yes"):
            rows[num] = True
        elif "no" in wired.lower():
            rows[num] = False
    return rows


def test_adr_table_is_parseable():
    """A table that stops parsing must fail loudly, not silently pass."""
    rows = _adr_table()
    assert rows, "ADR-010 implementation-status table did not parse"
    assert set(rows) == set(PROBES), (
        f"table rows {sorted(rows)} do not match probed rows {sorted(PROBES)}"
    )


@pytest.mark.parametrize("row", sorted(PROBES, key=int))
def test_claimed_status_matches_source(row: str):
    """Each row's yes/no must match what actually reaches the SDK."""
    surface = _source()
    actual = bool(set(PROBES[row]) & surface)
    claimed = _adr_table()[row]
    assert actual == claimed, (
        f"ADR-010 row {row} claims {'wired' if claimed else 'NOT wired'} "
        f"but the SDK surface says {'wired' if actual else 'NOT wired'}. "
        f"Probes: {PROBES[row]}. Surface: {sorted(surface)}"
    )


def test_row_13_is_not_wired_by_incidental_text():
    """Regression guard for the phantom gate that shipped a wrong count.

    `skills` appears 68 times across seven files under `src/dreamy/`, and none
    of the occurrences reaches an Agent SDK call: an argparse variable and its
    subcommand plumbing, a comment, an f-string footer, lockfile schema keys,
    Dreamy-local skill config and pinning, and the generated pin manifest. A
    grep-based probe counted raw hits and reported row 13 wired, making the
    ADR's headline 5/10 instead of the true 4/10. The count is descriptive, not
    load-bearing: the assertion below tests provenance, not arithmetic.

    This pins the distinction directly: the token exists in the raw text, and
    must NOT appear in the provenance-scoped surface.
    """
    raw = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
    surface = _source()
    assert "skills=" in raw, "fixture drift: the incidental occurrences are gone"
    assert "skills" not in surface, (
        "row 13 is in the SDK surface. If `skills=` is now genuinely passed to "
        "an Agent SDK call, update ADR-010's count and the G3 row of "
        "GATE-MATRIX.md together."
    )
    assert "setting_sources" not in surface

# What `src/dreamy/` actually contains, split the way ADR-010 row 13 splits it.
#
# INCIDENTAL: the token appears but means nothing -- an argparse variable, a
# comment, an f-string footer, lockfile schema keys.
# LOCAL_LOGIC: real Dreamy skill config and pinning. Not incidental, and calling
# it so would be its own overclaim.
#
# Neither reaches `claude_agent_sdk`, which is what makes row 13 unwired. The
# inventory and the disposition are asserted separately below, because the
# inventory can grow without the disposition changing.
#
# Filenames only, deliberately. The row cited line numbers until a reviewer
# showed the guard could confirm a cited line contains `skills` but not that it
# was the argparse variable rather than one of ten other `skills` lines in the
# same file. A citation the guard cannot falsify is decoration.
# Paths relative to src/dreamy/, never basenames. A reviewer showed basename
# flattening was a live bypass: `src/dreamy/tui/config.py` could acquire the
# token and the guard stayed green, because `config.py` was already an expected
# name. Widening the glob to every suffix enlarged that collision surface --
# `tui/skills.lock` became reachable the same way. `__init__.py` already
# duplicates across agents/, connectors/, resources/, tui/ and tui/views/, so
# the collision is not hypothetical.
INCIDENTAL_SKILLS_FILES = {
    "cli.py",
    "efficacy.py",
    "prompt_compiler.py",
    "skills_lock.py",
}
LOCAL_SKILL_LOGIC_FILES = {
    "config.py",
    "skill_pins.py",
}
# Non-Python artifacts carrying the token. An earlier guard globbed `*.py` while
# the ADR claimed "six files in src/dreamy/" unrestricted, so this generated pin
# manifest sat outside the guard's scope and outside the row's count. A guard
# narrower than the sentence it defends cannot falsify that sentence.
DATA_SKILLS_FILES = {
    "resources/skills.lock",
}
EXPECTED_SKILLS_FILES = (
    INCIDENTAL_SKILLS_FILES | LOCAL_SKILL_LOGIC_FILES | DATA_SKILLS_FILES
)


def _adr_row13_files() -> set[str]:
    """Paths named in ADR-010's row-13 evidence cell, relative to `src/dreamy/`.

    Read from the ADR rather than duplicated here. An earlier version kept its
    own copy of the citations and checked THAT against source, so mutating the
    ADR to cite `cli.py:999` left every test green -- it never opened the
    document it claimed to defend.

    Matches any suffix, not just `.py`: the row names a `.lock` artifact, and a
    `.py`-only pattern would silently drop it from the comparison. Directory
    prefixes are PRESERVED -- stripping them let a file in a subdirectory
    inherit a sibling's expected name and pass unnoticed.
    """
    for line in ADR.read_text().splitlines():
        if re.match(r"^\|\s*13\s*\|", line):
            return set(re.findall(r"`([A-Za-z0-9_./]+\.[A-Za-z0-9_]+)`", line))
    return set()


def test_the_three_subsets_are_disjoint():
    """A file belongs to exactly one bucket: incidental, local logic, or data."""
    buckets = {
        "incidental": INCIDENTAL_SKILLS_FILES,
        "local logic": LOCAL_SKILL_LOGIC_FILES,
        "data": DATA_SKILLS_FILES,
    }
    for a, b in itertools.combinations(sorted(buckets), 2):
        overlap = buckets[a] & buckets[b]
        assert not overlap, f"{sorted(overlap)} is both {a} and {b}"


def _files_carrying_token() -> set[str]:
    """Every file under src/dreamy/ whose bytes contain `skills`.

    Not `*.py`. The ADR row says "under src/dreamy/" with no suffix restriction,
    and a guard scoped narrower than the sentence it defends cannot falsify it --
    that gap is exactly how `resources/skills.lock` went uncounted. Compiled
    caches are excluded: they are build output, not source, and mirror the very
    .py files already counted.
    """
    carrying: set[str] = set()
    for path in SRC.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):  # pragma: no cover - binary assets
            continue
        if "skills" in text:
            carrying.add(path.relative_to(SRC).as_posix())
    return carrying


def test_adr_row13_inventory_matches_source():
    """Row 13's file inventory must equal what actually carries the token.

    Exact set: an eighth file acquiring `skills` would make the row silently
    incomplete, and a file losing it would leave a phantom entry.
    """
    named = _adr_row13_files()
    carrying = _files_carrying_token()
    assert named == carrying == EXPECTED_SKILLS_FILES, (
        f"ADR-010 row 13 names {sorted(named)}; src/dreamy/ carries "
        f"{sorted(carrying)}; guard expects {sorted(EXPECTED_SKILLS_FILES)}. "
        "All three must agree -- update the row and this guard together."
    )


def test_no_skills_file_reaches_the_sdk():
    """The actual disposition claim: none of them wires `skills`.

    Independent of the inventory above. The inventory can grow without changing
    the disposition; this assertion cannot change without flipping row 13.
    """
    surface = _source()
    assert "skills" not in surface, (
        "`skills` now reaches an Agent SDK call. Row 13 must flip to wired, and "
        "ADR-010's count plus the G3 row of GATE-MATRIX.md must change with it."
    )
    assert "setting_sources" not in surface


def test_no_unverifiable_line_citations_in_row13():
    """Row 13 must not reintroduce `file.py:NN` citations.

    Line numbers proved unverifiable in practice: a guard can confirm a cited
    line contains `skills`, but not that it is the argparse variable rather than
    one of ten other executable `skills` lines in the same file. A citation the
    guard cannot falsify is decoration.
    """
    for line in ADR.read_text().splitlines():
        if re.match(r"^\|\s*13\s*\|", line):
            # `/` and any suffix, not just bare `*.py`: the row names paths now,
            # so `src/dreamy/cli.py:42` and `resources/skills.lock:7` must be
            # caught too. A reviewer slipped a path-form citation past the
            # earlier bare-name pattern.
            cites = re.findall(r"`[A-Za-z0-9_./]+\.[A-Za-z0-9_]+:\d+`", line)
            assert not cites, (
                f"row 13 reintroduced unverifiable line citations: {cites}. "
                "Name the file only, or add a guard that can check the "
                "syntactic claim."
            )

def test_sdk_surface_size_is_pinned():
    """The docstring's "three imports and nine other members" must stay true.

    A reviewer found the header claiming seven when the measured surface held
    nine. Nothing executable depended on that number, so it drifted silently --
    the same phantom-gate shape this file exists to prevent. Asserting it makes
    the narrative falsifiable.
    """
    imports = {"query", "ClaudeAgentOptions", "ResultMessage"}
    surface = _sdk_surface()
    assert imports <= surface, (
        f"expected SDK imports missing from surface: {sorted(imports - surface)}"
    )
    others = surface - imports
    assert len(others) == 9, (
        f"SDK surface changed: {len(others)} non-import members {sorted(others)}, "
        "docstring says nine. Update both together."
    )




def _tool_registrations() -> set[str]:
    """Tool names DEFINED or REGISTERED in src/ — never merely mentioned.

    The SDK surface cannot answer this: Dreamy's own tools would be local
    definitions, not SDK imports. A separate instrument is required.

    Scoped deliberately. An earlier draft accepted `t in raw_text` as a
    fallback, which recreates the phantom gate this file exists to prevent: a
    comment or docstring naming a tool would have counted it as built. Only two
    things count here — a real `def`/`class` binding the name, and a string
    literal passed to a known SDK tool or MCP constructor.
    """
    canonical = {"tool", "SdkMcpTool", "create_sdk_mcp_server"}
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        # Resolve import aliases per file. Matching registrars by bare name let
        # `from claude_agent_sdk import tool as mktool` register a tool the
        # guard never saw -- an alias is exactly how a real bypass would look.
        registrars = set(canonical)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in canonical and alias.asname:
                        registrars.add(alias.asname)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                found.add(node.name)
            elif isinstance(node, ast.Call):
                fn = node.func
                name = None
                if isinstance(fn, ast.Name):
                    name = fn.id
                elif isinstance(fn, ast.Attribute):
                    name = fn.attr
                if name not in registrars:
                    continue
                for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
    return found


def test_eight_custom_tools_are_still_unbuilt():
    """The ADR states the eight tools are specified, not built.

    When they are built this fails, which is the point: the ADR, AGENT-PIPELINE.md,
    OBSERVABILITY.md and GATE-MATRIX.md all rest on their absence and must be
    revised together.
    """
    tools = [
        "dreamy_read_session_excerpt",
        "dreamy_list_project_sessions",
        "dreamy_get_git_evidence",
        "dreamy_get_findings",
        "dreamy_get_artifact_manifest",
        "dreamy_resolve_evidence_path",
        "dreamy_get_skill_manifest",
        "dreamy_submit_human_question",
    ]
    built = sorted(set(tools) & _tool_registrations())
    assert not built, (
        f"{built} now exist in src/. Update ADR-010, AGENT-PIPELINE.md, "
        "OBSERVABILITY.md, and the G3/G6 rows of GATE-MATRIX.md."
    )
