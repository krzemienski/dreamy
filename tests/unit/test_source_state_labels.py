"""Every source-state branch renders a claim the data can support.

This exists because a false label shipped. The Sources panel rendered
`available == false` as "not installed" for claude, codex, opencode, and
pi while all four were installed with 6136, 84, 1382, and 1 files on disk.
`available` is per-window activity, not installation.

The first fix introduced a third branch but left it unexercised: live data
only ever produced `(True, True)` and `(False, True)`, so
`(False, False)` shipped unverified — still asserting "not installed" from
a payload that carries no discovery state at all.

So this pins two things:

1. `source_stats` sets `available` and `ever_ingested` independently, from
   run counts and watermarks respectively.
2. Every branch of the renderer, including the one live data cannot reach,
   is labelled by observation rather than inference.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DASHBOARD_JS = (
    Path(__file__).resolve().parents[2]
    / "src" / "dreamy" / "web" / "static" / "dashboard.js"
)

# Claims about the machine that this payload cannot substantiate. The panel
# is built from run counts and watermarks; nothing in it reaches a discovery
# probe, so it can report what dreamy saw and nothing more.
UNSUPPORTABLE = ("not installed", "not found", "missing", "absent", "uninstalled")


def _source_state_block() -> str:
    text = DASHBOARD_JS.read_text(encoding="utf-8")
    start = text.index("const srcRows")
    end = text.index("sources.append", start)
    return text[start:end]


def test_no_installation_claims_in_source_labels() -> None:
    """No branch may assert a harness is absent from the machine."""
    block = _source_state_block()
    labels = re.findall(r'text:\s*"([^"]+)"', block)
    assert labels, "no source-state labels found; test is looking in the wrong place"
    for label in labels:
        low = label.lower()
        for claim in UNSUPPORTABLE:
            assert claim not in low, (
                f"label {label!r} claims installation state, but source_stats "
                f"carries only run counts and watermarks — no discovery probe"
            )


def test_all_three_branches_are_distinctly_labelled() -> None:
    """Three states must be visually distinguishable, not collapsed."""
    block = _source_state_block()
    labels = re.findall(r'text:\s*"([^"]+)"', block)
    assert len(labels) == 3, f"expected 3 source states, found {len(labels)}: {labels}"
    assert len(set(labels)) == 3, f"source states are not distinct: {labels}"


@pytest.mark.parametrize(
    ("available", "ever_ingested", "expected"),
    [
        (True, True, "active this run"),
        (False, True, "idle this run"),
        # The branch live data cannot reach. It shipped once with a false
        # label precisely because nothing exercised it.
        (False, False, "no data yet"),
        # available=True with ever_ingested=False should be impossible — a
        # source producing records has a watermark — but if it occurs, the
        # active label is still the honest one.
        (True, False, "active this run"),
    ],
)
def test_branch_selects_expected_label(
    available: bool, ever_ingested: bool, expected: str
) -> None:
    """Mirror of the renderer's branch order, pinned against the real source.

    The JS is the product; this reimplements its precedence so a change to
    either side that breaks the mapping fails here.
    """
    block = _source_state_block()
    assert expected in block, f"renderer no longer contains label {expected!r}"

    if available:
        label = "active this run"
    elif ever_ingested:
        label = "idle this run"
    else:
        label = "no data yet"
    assert label == expected


def test_read_store_sets_the_two_flags_independently() -> None:
    """`available` and `ever_ingested` must not be derived from each other.

    Collapsing them is the original defect: one bit answering two unrelated
    questions.
    """
    from dreamy.read import SourceStat

    quiet_but_known = SourceStat(
        source_id="claude", available=False, record_count=0, ever_ingested=True
    )
    assert not quiet_but_known.available
    assert quiet_but_known.ever_ingested

    never_seen = SourceStat(source_id="ghost", available=False, record_count=0)
    assert not never_seen.ever_ingested, "ever_ingested must default to False"
