"""
Equivalence self-check: brute-force reference matcher vs indexed matcher.

Reference implements the original semantics in this file. Covers tiers
1/2/3, ties, empty-project, no-match, and a gkey != native_id case.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dreamy.correlate import (
    Link,
    _build_router_match_indexes,
    _extract_router_id_candidates,
    _match_router_indexed,
    _SessionGroup,
    _stable_pick,
    normalize_fingerprint,
)
from dreamy.protocol import SourceRecord

WINDOW = 30_000  # DESIGN.md §3.3 -- default 30s
T0 = 1700000000000


def _g(source_id, native_id, project, model, started, ended=None,
       members=None, fps=None, raw_ids=None):
    g = _SessionGroup(source_id=source_id, native_id=native_id)
    g.project_path = project or ""
    g.model = model or ""
    g.started_ms = started
    g.ended_ms = ended
    if members:
        g.member_native_ids.update(members)
    if raw_ids:
        g.member_raw_ids.update(raw_ids)
    if fps:
        g.fingerprints.update(fps)
    return g


def _r(sid, nid, model, project, ts, fp="", raw_meta=None):
    rec = SourceRecord(
        source_id=sid, native_id=nid, timestamp_ms=ts,
        record_type="router_request", model=model or "",
        project_path=project or "", content_excerpt=fp,
    )
    if raw_meta:
        rec.raw_meta = raw_meta
    return rec


def _interval_distance(ts_ms, started_ms, ended_ms):
    """Distance from ts to the closed interval [started, ended]. 0 when inside.

    The indexed matcher measures to the SPAN, not the midpoint. Midpoint
    distance rejected requests landing inside long sessions (measured:
    16,662 at a 30s window) and accepted requests far outside short ones.
    """
    st = started_ms
    en = ended_ms if ended_ms is not None else started_ms
    if ts_ms < st:
        return st - ts_ms
    if ts_ms > en:
        return ts_ms - en
    return 0


def _reference_match(router, groups, window_ms):
    """Brute-force reference: walks every group, applies tiers in order.

    Mirrors DESIGN.md §3.3 exactly -- T1 `exact`, T2 `high`, T3 `medium` --
    and the indexed matcher's reason strings. This reference previously
    carried its own vocabulary (`high`/`medium`/`low`) and its own midpoint
    window, so it reported 7 mismatches against correct code and could not
    detect a real regression.
    """
    router_ids = _extract_router_id_candidates(router)
    router_fp = normalize_fingerprint(router.content_excerpt) if router.content_excerpt else ""
    window_s = window_ms // 1000

    tier1 = []
    for gkey, group in groups.items():
        candidate_ids = group.member_native_ids | group.member_raw_ids | {group.native_id}
        overlap = router_ids & candidate_ids
        if overlap:
            tier1.append((gkey, group, sorted(overlap)[0]))
    if tier1:
        gkey, group, matched_id = sorted(tier1, key=lambda t: (t[1].started_ms, t[0][0], t[0][1]))[0]
        return Link(
            left_id=f"{group.source_id}:{group.native_id}",
            right_id=router.native_id, tier=1, confidence="exact",
            reason=f"exact native id overlap on {matched_id} → session in {group.project_path}",
        )

    # T2 is keyed on model alone; the project constraint is satisfied by the
    # matched GROUP carrying a project, not by the router record -- 9router's
    # usageHistory has no project column, so requiring router.project_path
    # could never match.
    tier2 = []
    if router.model:
        for gkey, group in groups.items():
            if not group.project_path or group.model != router.model:
                continue
            if _interval_distance(router.timestamp_ms, group.started_ms, group.ended_ms) <= window_ms:
                tier2.append((gkey, group))
    if tier2:
        projects = {g.project_path for _, g in tier2}
        if len(projects) == 1:
            gkey, group = _stable_pick(tier2)
            return Link(
                left_id=f"{group.source_id}:{group.native_id}",
                right_id=router.native_id, tier=2, confidence="high",
                reason=f"model={router.model} + project={group.project_path} within {window_s}s",
            )

    tier3 = []
    if router_fp:
        for gkey, group in groups.items():
            if not group.project_path:
                continue
            if _interval_distance(router.timestamp_ms, group.started_ms, group.ended_ms) > window_ms:
                continue
            if router_fp in group.fingerprints:
                tier3.append((gkey, group))
    if tier3:
        projects = {g.project_path for _, g in tier3}
        if len(projects) == 1:
            gkey, group = _stable_pick(tier3)
            return Link(
                left_id=f"{group.source_id}:{group.native_id}",
                right_id=router.native_id, tier=3, confidence="medium",
                reason=f"prompt fingerprint + project={group.project_path} within {window_s}s",
            )
    return None


def _build_fixture():
    g1 = _g("claude", "sess-uuid-1", "/tmp/proj-a", "claude-opus-5",
            T0, ended=T0 + 600_000,
            members={"turn-1", "turn-2"}, raw_ids={"req-1"},
            fps={"fp hello", "fp second"})
    g2 = _g("omp", "sess-uuid-2", "/tmp/proj-b", "cc/claude-opus-5",
            T0 + 1_000_000, ended=T0 + 1_600_000,
            members={"m1"}, raw_ids={"req-2"},
            fps={"fp omp"})
    g3 = _g("pi", "sess-uuid-3", "/tmp/proj-a", "claude-opus-5",
            T0 + 2_000_000, ended=T0 + 2_600_000,
            raw_ids={"req-3"}, fps={"fp another"})
    g1b = _g("claude", "sess-uuid-1b", "/tmp/proj-a", "claude-opus-5",
             T0 + 100_000, ended=T0 + 500_000,
             members={"turn-1b"}, fps={"fp hello"})
    g_empty = _g("claude", "sess-empty", "", "claude-opus-5",
                 T0, ended=T0 + 600_000,
                 members={"t_empty"}, fps={"fp empty"})
    # Grouping key != group.native_id: gkey is a file path; the original
    # matcher used group.native_id for left_id. The indexed matcher must
    # read group.native_id from group_attrs (the group_ref slot).
    g_diffkey = _g("claude", "s1", "/tmp/proj-d", "claude-opus-5",
                   T0 + 300_000, ended=T0 + 800_000,
                   members={"t-diff"})
    diffkey_gkey = ("claude", str(Path("/tmp") / "session-99.jsonl"))
    groups = {
        ("claude", "s1"): g1,
        ("omp", "s2"): g2,
        ("pi", "s3"): g3,
        ("claude", "s1b"): g1b,
        ("claude", "s_empty"): g_empty,
        diffkey_gkey: g_diffkey,
    }
    # A long session: 40 min. Its midpoint is ~20 min from either edge, far
    # beyond any 30s window -- so midpoint distance rejected every request
    # inside it. Interval distance accepts them (distance 0).
    g_long = _g("claude", "sess-long", "/tmp/proj-long", "long-model",
               T0 + 5_000_000, ended=T0 + 7_400_000,
               members={"t-long"}, fps={"fp long"})
    groups[("claude", "s_long")] = g_long
    return groups


CASES = [
    # 0. Tier 1: native id overlap (router.native_id == group.native_id)
    ("tier1-native", _r("nine_router", "sess-uuid-1", "claude-opus-5",
                         "/tmp/proj-a", T0 + 300_000)),
    # 1. Tier 1: member turn id
    ("tier1-member", _r("nine_router", "turn-1", "claude-opus-5",
                         "/tmp/proj-a", T0 + 200_000)),
    # 2. Tier 1: raw_meta id -- MUST set raw_meta dict, not content_excerpt
    ("tier1-rawid", _r("nine_router", "anything", "claude-opus-5",
                        "/tmp/proj-a", T0 + 100_000,
                        raw_meta={"request_id": "req-1"})),
    # 3. Tier 2 tie: g1 vs g1b both match model+project+time. s1b started
    #    earlier (T0+100k vs T0) so s1b wins.
    ("tier2-tie", _r("nine_router", "router-x", "claude-opus-5",
                      "/tmp/proj-a", T0 + 200_000)),
    # 4. Tier 2 wrong model: should not match g1/g3 (different model)
    ("tier2-wrongmodel", _r("nine_router", "router-y", "cc/claude-opus-5",
                             "/tmp/proj-b", T0 + 1_300_000)),
    # 5. Tier 3 fp: wrong fingerprint text must NOT match via tier 2 or
    #    tier 3 (neither reference nor indexed should find anything --
    #    equivalence must still hold on a negative case).
    ("tier3-fp-wrongtext", _r("nine_router", "router-z", "totally-wrong-model",
                               "/tmp/proj-a", T0 + 200_000, fp="hello")),
    # 6. No match: outside window
    ("nomatch-outside", _r("nine_router", "router-q", "claude-opus-5",
                            "/tmp/proj-a", T0 + 10_000_000)),
    # 7. Tier 3 fp: must NOT match via tier 2. Wrong model forces tier 3.
    #    The reference stores the normalized fingerprint in
    #    `group.fingerprints`, so the router content_excerpt must produce
    #    the SAME normalized string. The group has "fp hello" in its
    #    fingerprints set; router content "fp hello" normalizes via
    #    whitespace collapse to "fp hello" (single token). In practice,
    #    group.fingerprints are populated with the already-normalized
    #    excerpt of each turn; for this synthetic test we use the same
    #    literal string.
    ("tier3-fp", _r("nine_router", "router-z", "totally-wrong-model",
                     "/tmp/proj-a", T0 + 200_000, fp="fp hello")),
    ("tier3-empty-router", _r("nine_router", "router-empty",
                               "totally-wrong-model", "",
                               T0 + 200_000, fp="hello")),
    # 8. Tier 3 with empty-project group -- group fp "fp empty" should NOT
    #    match a router with non-empty project at "proj-a" even when the
    #    fingerprint text itself is wrong (equivalence must hold here too).
    ("tier3-empty-group-wrongtext", _r("nine_router", "router-r",
                                        "totally-wrong-model", "/tmp/proj-a",
                                        T0 + 200_000, fp="empty")),
    # 9. Tier 3 with empty-project group -- group fp "fp empty" should NOT
    #    match a router with non-empty project at "proj-a" (no tier3_index
    #    entry for (fp="fp empty", project="proj-a") AND the empty-project
    #    group is excluded from tier3_index by contract.
    ("tier3-empty-group", _r("nine_router", "router-r",
                              "totally-wrong-model", "/tmp/proj-a",
                              T0 + 200_000, fp="fp empty")),
    #    left_id must use group.native_id ("s1"), NOT the gkey file path.
    ("tier1-diffkey", _r("nine_router", "t-diff", "claude-opus-5",
                          "/tmp/proj-d", T0 + 500_000)),
    # 10. INSIDE a long session's span, but ~19 min from its midpoint.
    #     Midpoint distance rejects this; interval distance accepts it
    #     (distance 0). This is the 16,662-request class measured on live
    #     data at a 30s window.
    ("t2-inside-long-span", _r("nine_router", "router-long-in", "long-model",
                                "/tmp/proj-long", T0 + 6_200_000)),
    # 11. 60s AFTER the same session ended -- outside a 30s window, so T4.
    #     Guards the opposite direction: interval distance must not become
    #     a licence to match anything near a long session.
    ("t4-after-end-60s", _r("nine_router", "router-long-out", "long-model",
                             "/tmp/proj-long", T0 + 7_460_000)),
]


@pytest.fixture(scope="module")
def indexes():
    groups = _build_fixture()
    return groups, _build_router_match_indexes(groups, window_ms=WINDOW)


@pytest.mark.parametrize("label,rec", CASES, ids=[c[0] for c in CASES])
def test_indexed_matcher_matches_reference(indexes, label, rec):
    groups, idx = indexes
    ref = _reference_match(rec, groups, window_ms=WINDOW)
    got = _match_router_indexed(rec, idx, window_ms=WINDOW)
    ref_repr = (ref.tier, ref.left_id, ref.confidence, ref.reason) if ref else None
    got_repr = (got.tier, got.left_id, got.confidence, got.reason) if got else None
    assert got_repr == ref_repr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
