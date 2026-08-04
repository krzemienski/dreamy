"""Truncation must never leave a severed path behind.

The cold-start gate (R6a) resolves every absolute path it finds in a
generated artifact. A blind `text[:limit]` cuts mid-token, so
`/home/<op>/Desktop/my-hyphenated-project` became
`/home/<op>/Desktop/my-hyphenated-` — indistinguishable, to a reader or a
checker, from a citation to a file that does not exist. Four such fragments
failed the gate across three artifacts.

Truncation itself is right: these are session excerpts and bounding them is
the point. What is wrong is leaving behind something that reads as
addressable.

`_clip` was first shipped with only ad-hoc checks. These are the runnable
guards, covering the helper directly and every renderer that truncates
session text.
"""
from __future__ import annotations

import re

import pytest

from dreamy.evidence_projection import (
    _clip,
    _evidence_citations,
    _intent,
    _rank_next_tasks,
    build,
)

# Deliberately broader than the product's own pattern: this must catch a
# fragment the renderer thinks is fine, so it cannot share the bug.
PATHLIKE = re.compile(r"/(?:Users|home|opt|usr|var|tmp|private)/[A-Za-z0-9._/\-]*")

LONG_PATH = "/Users/someone/Desktop/a-project-with-a-long-hyphenated-name/src/mod.py"


def _severed_paths(text: str, originals: tuple[str, ...]) -> list[str]:
    """Path-like runs that are a strict prefix of an original — i.e. cut."""
    found = [m.group() for m in PATHLIKE.finditer(text)]
    return [
        f for f in found
        if any(o.startswith(f) and f != o for o in originals)
    ]


@pytest.mark.parametrize("limit", list(range(8, 96, 7)))
def test_no_limit_leaves_a_severed_path(limit: int) -> None:
    """Swept across cut positions, not one hand-picked case.

    The original bug survived a hand-written check because the chosen limit
    happened to fall outside the path.
    """
    text = f"Resume work on {LONG_PATH} before continuing the migration"
    out = _clip(text, limit)
    assert not _severed_paths(out, (LONG_PATH,)), f"limit={limit} produced {out!r}"


def test_whole_path_is_preserved_when_it_fits() -> None:
    """Over-clipping is its own defect — a citation that loses its path is useless."""
    text = f"see {LONG_PATH} now"
    assert _clip(text, len(text) + 10) == text
    kept = _clip(text, len(f"see {LONG_PATH}") + 1)
    assert LONG_PATH in kept


def test_prose_is_not_mangled() -> None:
    """No path, no special handling."""
    text = "a" * 40 + " and then some ordinary prose continues here"
    out = _clip(text, 50)
    assert out.startswith("a" * 40)
    assert len(out) <= 51


def test_short_text_is_returned_unchanged() -> None:
    assert _clip("short", 100) == "short"


@pytest.mark.parametrize("limit", list(range(8, 44, 4)))
def test_urls_are_not_severed(limit: int) -> None:
    """A cut URL is as misleading as a cut path.

    The first version of this test asserted
    `"127.0.0.1:8791/api" not in out or url in out`, which is satisfied
    whenever the cut lands anywhere else — it passed with the boundary
    logic disabled entirely. Now every cut position is swept and the
    surviving text must contain either the whole URL or no fragment of it.
    """
    url = "http://127.0.0.1:8791/api/v1/overview"
    out = _clip(f"open {url} to inspect", limit)
    if url in out:
        return
    # No proper prefix of the URL beyond the scheme may survive: that is
    # exactly the misleading fragment.
    for n in range(len("http://") + 1, len(url)):
        assert url[:n] not in out, f"limit={limit} left fragment {url[:n]!r} in {out!r}"


# --- renderer call sites -------------------------------------------------
#
# `_clip` being correct is necessary but not sufficient: the original defect
# was a call site that never used it. Each renderer that truncates session
# text is exercised with input whose path lands astride the cut.


def test_evidence_citations_do_not_sever_paths() -> None:
    finding = {
        "evidence": {
            "citations": [
                {"ts_ms": 0, "role": "user", "excerpt": "x" * 140 + " " + LONG_PATH},
            ]
        }
    }
    for line in _evidence_citations(finding):
        assert not _severed_paths(line, (LONG_PATH,)), line


def test_intent_does_not_sever_paths() -> None:
    episode = {"original_intent": "y" * 120 + " " + LONG_PATH}
    out = _intent(episode, 140)
    assert not _severed_paths(out, (LONG_PATH,)), out


def test_intent_without_limit_is_untouched() -> None:
    """No limit means no truncation, so the full path must survive."""
    episode = {"original_intent": f"work on {LONG_PATH}"}
    assert LONG_PATH in _intent(episode)


def test_next_task_titles_do_not_sever_paths() -> None:
    """Covers both title paths: the `Finish:` intent and the `Follow up:` seed."""
    episodes = [
        {
            "completion_status": "in_progress",
            "original_intent": "z" * 120 + " " + LONG_PATH,
            "started_ms": 0,
            "ended_ms": 0,
        }
    ]
    seeds = ["w" * 120 + " " + LONG_PATH]
    for item in _rank_next_tasks([], episodes, seeds, span_end_ms=0):
        for field in ("title", "evidence"):
            value = str(item.get(field, ""))
            assert not _severed_paths(value, (LONG_PATH,)), f"{field}: {value}"


def test_build_pending_items_do_not_sever_paths(tmp_path) -> None:
    """Covers the `pending_items` seed branch, reachable only through `build()`.

    This is the gap that mattered: mutating that line back to a blind
    `[:200]` slice left the whole clip suite GREEN, because every other test
    exercises a helper directly and none of them renders this branch. A
    guard that cannot fail is not a guard.
    """
    seed = "v" * 150 + " " + LONG_PATH
    out = build(
        project={"id": "p1", "path": "/tmp/p1", "name": "p1"},
        analysis_evidence={
            "findings": [],
            "episodes": [],
            "sessions": [],
            "next_task_seeds": [seed],
            "git": {},
            "original_intent": "",
            "anchor_ms": 0,
            "now_ms": 0,
        },
        output_dir=tmp_path,
        project_slug="p1",
    )
    rendered = "\n".join(str(v) for v in out.values())
    assert not _severed_paths(rendered, (LONG_PATH,)), rendered[:400]
