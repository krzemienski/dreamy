"""Requirement-ID traceability between the gate matrix and the specification.

Hand-checking cross-document ID references is exactly the kind of thing that
drifts silently: a typo, a range abbreviation, or an ID deleted from the spec
leaves the matrix pointing at nothing while still *looking* traceable. These
tests make that mechanical.

Runs in the default suite, deliberately. Traceability is a completion-gate
property, and a standalone script nobody invokes proves nothing.
"""

from __future__ import annotations

import collections
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "specifications" / "SPEC-DREAMY-V2.md"
MATRIX = REPO / "docs" / "acceptance" / "GATE-MATRIX.md"

# A requirement ID is PREFIX-NNN. Anchored so "SDK-004 … SDK-010" cannot be
# mistaken for two valid tokens with legal punctuation between them.
_ID_RE = re.compile(r"\b([A-Z]{3,6}-\d{3})\b")
_GATE_ROW_RE = re.compile(r"^\|\s*(G\d+)\s*\|([^|]*)\|")

# Range punctuation that must never appear inside a Req IDs cell: an
# abbreviated span is not a stable reference to each requirement it covers.
_RANGE_TOKENS = ("…", "...", "–", "—", " through ", " to ")

_UNASSIGNED = "UNASSIGNED"


def _spec_ids() -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    ids: set[str] = set()
    # Only IDs defined in a requirements table row (| ID | Requirement |) count
    # as normative. A prose mention is not a definition.
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        m = _ID_RE.fullmatch(cells[0])
        if m and cells[1] and not set(cells[1]) <= {"-"}:
            ids.add(m.group(1))
    return ids


def _matrix_rows() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        m = _GATE_ROW_RE.match(line)
        if m:
            rows.append((m.group(1), m.group(2).strip()))
    return rows


def test_spec_and_matrix_exist():
    assert SPEC.is_file(), f"missing {SPEC.relative_to(REPO)}"
    assert MATRIX.is_file(), f"missing {MATRIX.relative_to(REPO)}"


def test_spec_defines_requirements():
    ids = _spec_ids()
    assert ids, "specification defines no requirement IDs"


def test_spec_ids_are_unique():
    text = SPEC.read_text(encoding="utf-8")
    defined = [
        c.strip()
        for line in text.splitlines()
        if line.startswith("|")
        for c in [line.strip().strip("|").split("|")[0]]
        if _ID_RE.fullmatch(c.strip())
    ]
    dupes = [i for i, n in collections.Counter(defined).items() if n > 1]
    assert not dupes, f"requirement IDs defined more than once: {sorted(dupes)}"


def test_matrix_has_gate_rows():
    rows = _matrix_rows()
    assert rows, "gate matrix declares no gate rows"


@pytest.mark.parametrize("token", _RANGE_TOKENS)
def test_matrix_uses_no_range_abbreviations(token):
    offenders = [g for g, cell in _matrix_rows() if token in cell]
    assert not offenders, (
        f"gate(s) {offenders} abbreviate requirement IDs with {token!r}; "
        "every covered ID must be listed explicitly"
    )


def test_every_matrix_id_is_defined_in_spec():
    defined = _spec_ids()
    missing: dict[str, list[str]] = {}
    for gate, cell in _matrix_rows():
        if _UNASSIGNED in cell:
            continue
        cited = set(_ID_RE.findall(cell))
        absent = sorted(cited - defined)
        if absent:
            missing[gate] = absent
    assert not missing, (
        "gate matrix cites requirement IDs with no normative definition in "
        f"SPEC-DREAMY-V2.md: {missing}"
    )


def test_unassigned_cells_cite_no_ids():
    """UNASSIGNED means the mapping is an open gap.

    A cell claiming both UNASSIGNED and a concrete ID is contradictory, and the
    ambiguity would let a partially-traced gate read as fully traced.
    """
    bad = {
        gate: sorted(_ID_RE.findall(cell))
        for gate, cell in _matrix_rows()
        if _UNASSIGNED in cell and _ID_RE.findall(cell)
    }
    assert not bad, f"gates marked UNASSIGNED but also citing IDs: {bad}"


def test_unassigned_gates_are_blocked():
    """A gate whose requirements are untraced must not be claimable.

    Without this, a gate could be marked PASS while nothing normative defines
    what it was supposed to prove.
    """
    offenders = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        m = _GATE_ROW_RE.match(line)
        if not m or _UNASSIGNED not in m.group(2):
            continue
        if "BLOCKED" not in line.upper():
            offenders.append(m.group(1))
    assert not offenders, (
        f"gate(s) {offenders} have UNASSIGNED requirement IDs but are not BLOCKED"
    )


def test_no_gate_has_unassigned_requirements():
    """Every gate MUST map to explicit requirement IDs.

    Being BLOCKED does not excuse an untraced gate: "we cannot evaluate this
    yet" and "we never said what this proves" are different failures, and the
    second one hides. This test fails while any mapping is missing, so the gap
    is visible in the suite rather than only in a document nobody re-reads.
    """
    untraced = [gate for gate, cell in _matrix_rows() if _UNASSIGNED in cell]
    assert not untraced, (
        f"gate(s) {untraced} have no assigned requirement IDs. Assign them in "
        "SPEC-DREAMY-V2.md and cite them in GATE-MATRIX.md; do not relax this "
        "test to make the suite green."
    )
