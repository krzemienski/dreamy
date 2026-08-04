"""Citation-resolution self-check for the acceptance evidence tree.

D27 prevention item (`VG-2/D27-citation-remap.md`): doc consolidation
silently invalidated a `<file>:<line>` citation inside a sealed verdict
(`SPEC-dreamy.md:1235`, dead after the spec shrank from 1,445 to 322 lines).
Line-number citations are fragile -- they die the moment a file is
reflowed, split, or renumbered, and nothing catches it mechanically. This
module is that catch.

It scans every regular file under the acceptance evidence tree for
`<path>:<line>` and `<path>:<start>-<end>` references, and verifies each
resolves: the cited path exists as a file, and the cited line (or the full
span of a range) is within that file's line count.

No extension allowlist gates which files get scanned. Every regular file
is opened; files that sniff as binary (a NUL byte in the first 8 KiB, or a
UTF-8 decode failure) are counted and reported separately rather than
silently omitted. A single line that exceeds a defensive size cap is
likewise reported as unscannable for that file, not silently dropped --
this only matters for pathological single-line files (none observed in
this tree; the largest lines seen are ordinary prose/JSON rows).

Every occurrence is resolved independently against the file it was found
in -- not deduplicated on anchor text alone. Two evidence directories can
cite the identical literal string (e.g. `run.py:216`) and mean two
different files (a per-gate frozen copy vs. the shipping module), so
grouping keys on the resolved outcome (including which concrete path it
resolved to), not just the anchor text.

Path resolution tries, in order, and stops at the first hit:
  1. as an absolute path
  2. relative to the directory of the file the citation was found in
  3. relative to the acceptance evidence root
  4. relative to the repo root
  5. a basename match anywhere under the repo root, EXCLUDING vendor/build
     dirs (.venv, .git, node_modules, __pycache__, .pytest_cache,
     .mypy_cache) -- accepted ONLY if exactly one file in the repo carries
     that basename. Two or more matches is left unresolved as ambiguous;
     guessing which one the author meant would be exactly the kind of
     silent remap D27 warns against.

Known sealed citations are never silently dropped, but they are not
treated as a fresh failure either. Any markdown file anywhere in the
acceptance tree that contains a table with a header cell matching
"cited (dead)" / "resolves to (live)" (the convention
`D27-citation-remap.md` established) is read as a remap-table source: its
"Cited (dead)" column values are extracted as documented-dead anchors. An
anchor that fails to resolve AND appears in some remap table is filed
under `sealed_documented`, not `unresolved` -- the verdict for the
artifact that cites it already stands (see the remap note itself); this
module only tracks that the dead citation remains recorded rather than
silently edited away.

Anything unresolved that is NOT in a remap table IS a real finding.

Read-only. Only ever reports paths, line numbers, and counts -- never file
content, so it cannot leak secrets embedded in evidence artifacts.

Real-system acceptance check: requires the live evidence tree at
`~/.local/share/dreamy/acceptance`. Skipped when that directory does not
exist. When it does exist, the pytest wrapper below asserts the tree's
own PASS/FAIL verdict faithfully -- an unresolved, undocumented citation
in the evidence tree is a genuine defect this test is supposed to
surface, not something to be waived away.

Usage as a standalone CLI:
    python3 tests/integration/test_citation_selfcheck.py [acceptance_root] [--json]

Exit codes:
    0  every anchor resolves, or is sealed/documented via a remap table
    1  at least one anchor is unresolved and not sealed/documented
    2  usage error
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

# The acceptance-runs trust base is deliberately NOT defined or imported here.
# An earlier version recomputed it from `expanduser()` and passed it to the
# validator explicitly, so this gate carried the exact `$HOME` relocation
# defect the production default was fixed to close — while looking correct in
# isolation. `validate_run_manifest`'s own default is the single source of
# truth; anything this file passed could only diverge from it.

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ACCEPTANCE_ROOT = Path("~/.local/share/dreamy/acceptance").expanduser()

# The current gate reads exactly one run, named by its manifest. Pointing the
# gate at a bare directory path was rejected as a design: an empty or wrong
# directory yields zero anchors, zero failures, and a false PASS. A manifest
# must declare its run_id and the artifacts it covers, and every declared
# artifact is hash-verified before the sweep is trusted.
CURRENT_RUN_MANIFEST_ENV = "DREAMY_CURRENT_RUN_MANIFEST"

pytestmark = pytest.mark.acceptance

skip_reason = f"no acceptance evidence tree at {DEFAULT_ACCEPTANCE_ROOT}"

# Extensions a citation TARGET is allowed to end in. Gates what the anchor
# regex recognizes as a path when scanning file content -- doubles as a
# guard against matching version strings, timestamps, or JSON numeric
# fields that happen to contain a colon. Does NOT gate which files get
# scanned; every regular file under the tree is opened (see
# _iter_scan_files), regardless of its own extension.
_ANCHOR_EXTS = r"py|md|json|txt|sql|sh|ya?ml|cfg|ini|log"

_SKIP_DIR_NAMES = {
    ".venv", ".git", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache",
}

# Executable source files are NOT citation-bearing artifacts and must not be
# scanned. A helper script under the evidence tree contains ordinary Python
# string literals -- e.g. VG-4/D24-D31-reverify-prose-check.py:10 sets
# VG4 = "/Users/.../acceptance/VG-4" and interpolates f"{VG4}/probes.json:6"
# -- which are code, not claims about evidence. Scanning them produced 130 of
# 164 "dead anchors", none of which were real citations.
#
# Those fragments cannot be rescued by smarter resolution: re-rooting the
# captured tail at the evidence root resolves 18 uniquely, 30 AMBIGUOUSLY, and
# 82 not at all, so 112 of 130 would be guessed wrong or blessed arbitrarily.
# Treating a code string as a citation both invents failures and, once
# "resolved", hides real ones.
#
# A citation that genuinely needs checking belongs in a document or a manifest,
# which are still scanned in full.
#
# Deliberately minimal: only Python source. Compiled artifacts (.so, .dylib,
# .pyc) are already excluded by the binary sniff, and __pycache__ by
# _SKIP_DIR_NAMES, so listing them here would add no coverage while making the
# exclusion look broader than the evidence justifies. Every additional suffix
# is coverage removed, so each one must be earned.
_NON_CITATION_SUFFIXES = {".py"}

_BINARY_SNIFF_BYTES = 8192

# Window size for scanning a single very long line (e.g. minified JSON).
# Bounds peak memory per scan; it is NOT a skip threshold.
_MAX_LINE_BYTES = 500_000

# Longest an anchor can plausibly be: a path component plus ":line-line".
# Consecutive scan windows overlap by this much so an anchor straddling a
# window boundary is still matched whole by the following window. Any value
# >= the true maximum anchor length is correct; this is deliberately generous.
_ANCHOR_MAX_BYTES = 4096

_ANCHOR_RE = re.compile(
    r"(?<![\w./])"
    r"([A-Za-z0-9_./-]+\.(?:" + _ANCHOR_EXTS + r"))"
    r":([1-9]\d*)(?:-([1-9]\d*))?"
    r"(?!\d)"
)

_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _walk(root: Path):
    """os.walk-alike over pathlib, pruning vendor/build dirs."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        yield Path(dirpath), dirnames, filenames


def _sniff(p: Path) -> str | None:
    """Classify a file before scanning. Returns None if it is scannable
    text; otherwise a short reason string ("binary" or "unreadable").

    A NUL byte in the first chunk marks binary content directly. Absent
    that, the same chunk is decode-tested as UTF-8 -- a decode failure
    means the file isn't the text-with-occasional-non-ASCII-byte case
    `errors="replace"` is meant for, it's actually binary content that
    happens to lack an early NUL (e.g. some compressed or packed formats),
    so it is classified as binary rather than silently scanned lossily."""
    try:
        with p.open("rb") as f:
            chunk = f.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return "unreadable"
    if b"\x00" in chunk:
        return "binary"
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return None


def _iter_scan_files(root: Path):
    """Every regular file under root, classified as scannable or skipped.

    Yields (path, skip_reason). skip_reason is None for files that will be
    scanned; otherwise "binary", "unreadable", or "source_not_citation".

    Executable source files are reported with "source_not_citation" rather
    than silently dropped, so the count still appears in the sweep result and
    the exclusion stays auditable. Everything else -- markdown, JSON
    manifests, logs, text -- is scanned in full."""
    for dirpath, _dirnames, filenames in _walk(root):
        for fn in filenames:
            p = dirpath / fn
            if not p.is_file():
                continue
            if p.suffix in _NON_CITATION_SUFFIXES:
                yield p, "source_not_citation"
                continue
            yield p, _sniff(p)


def _line_count(p: Path) -> int | None:
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def _normalize_anchor(m: re.Match) -> str:
    path, start, end = m.group(1), m.group(2), m.group(3)
    return f"{path}:{start}-{end}" if end else f"{path}:{start}"


def _scan_file_for_anchors(p: Path):
    """Yields (line_no, match, oversized) tuples for every citation-shaped
    anchor found in `p`.

    Reads fixed-size chunks straight from the file handle, never whole lines.
    Iterating `for line in f` would defeat the point: the opencode model cache
    is a single 3.3 MB line, so line iteration materialises the entire line
    before any windowing could bound it.

    Long lines are NOT skipped. Minified JSON is a legitimate and common
    evidence shape, and skipping it produced exactly the coverage gap this
    checker exists to detect. Consecutive chunks overlap by _ANCHOR_MAX_BYTES
    so an anchor straddling a chunk boundary is still matched whole; matches
    are deduped by absolute file offset because the overlap deliberately
    re-reads the tail of the previous chunk. Peak memory is bounded by
    _MAX_LINE_BYTES regardless of line or file length.

    Line numbers are reconstructed by counting newlines consumed so far, so
    they stay correct across chunk boundaries.

    `errors="replace"` means invalid UTF-8 is substituted rather than raising
    mid-stream, so the only exception this can still raise is OSError -- left
    to the caller to catch.

    The third tuple element is retained (always False) so callers that unpack
    three values keep working; `oversized_lines` therefore stays empty rather
    than being dropped from the result contract."""
    step = _MAX_LINE_BYTES - _ANCHOR_MAX_BYTES
    if step <= 0:  # pragma: no cover - guards a bad constant edit
        raise ValueError("_MAX_LINE_BYTES must exceed _ANCHOR_MAX_BYTES")

    with p.open(encoding="utf-8", errors="replace") as f:
        buf = ""
        newlines_before = 0    # newlines consumed strictly before buf[0]
        at_eof = False

        while not at_eof:
            chunk = f.read(step)
            at_eof = not chunk
            buf += chunk

            # A match touching the unread edge may be truncated: a boundary
            # falling inside "foo.py:123" can match "foo.py:1" and report the
            # wrong line. Only emit matches that END strictly before the safe
            # cutoff, where the remaining tail is long enough to complete any
            # anchor. At EOF nothing more is coming, so everything is safe.
            # Because each match is emitted exactly once -- when it is first
            # known complete -- no dedupe set is needed, and memory stays
            # bounded by the buffer rather than growing with the file.
            safe_end = len(buf) if at_eof else max(0, len(buf) - _ANCHOR_MAX_BYTES)

            consumed = 0
            for m in _ANCHOR_RE.finditer(buf):
                if m.end() > safe_end:
                    break
                yield newlines_before + buf.count("\n", 0, m.start()) + 1, m, False
                consumed = m.end()

            # Drop everything definitively behind us: emitted matches plus any
            # text that can no longer begin an anchor reaching into the tail.
            cut = max(0, min(consumed, len(buf) - _ANCHOR_MAX_BYTES)) if not at_eof else len(buf)
            if cut > 0:
                newlines_before += buf.count("\n", 0, cut)
                buf = buf[cut:]

            # The buffer must still shrink when a chunk contains no match at
            # all, or a match-free 3 MB line would grow it without bound.
            if not at_eof and len(buf) > _MAX_LINE_BYTES:
                cut = len(buf) - _ANCHOR_MAX_BYTES
                newlines_before += buf.count("\n", 0, cut)
                buf = buf[cut:]


def _extract_remap_table_anchors(p: Path) -> list[str]:
    """Anchors listed under a "Cited (dead)"-style column in any markdown
    table, streamed line by line. Generalizes the convention
    `D27-citation-remap.md` established, rather than hardcoding that one
    filename."""
    anchors: list[str] = []
    in_table = False
    expect_sep = False
    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _TABLE_ROW_RE.match(line)
            if not m:
                in_table = False
                expect_sep = False
                continue
            cells = [c.strip() for c in m.group(1).split("|")]
            if expect_sep:
                expect_sep = False
                if _TABLE_SEP_RE.match(line):
                    continue
                # Not actually a separator row (malformed table); fall
                # through and treat this row as ordinary data below.
            header_lower = [c.lower() for c in cells]
            is_remap_header = (
                len(header_lower) >= 2
                and "cited" in header_lower[0]
                and "dead" in header_lower[0]
                and "resolves" in header_lower[1]
            )
            if is_remap_header:
                in_table = True
                expect_sep = True
                continue
            if in_table and cells:
                for anchor_m in _ANCHOR_RE.finditer(cells[0]):
                    anchors.append(_normalize_anchor(anchor_m))
    return anchors


def _resolve(cited_path: str, citing_file: Path, acceptance_root: Path,
             repo_root: Path) -> tuple[Path | None, str | None]:
    """Returns (resolved_path, error_reason). error_reason is None on success."""
    p = Path(cited_path)
    candidates: list[Path] = []
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(citing_file.parent / p)
        candidates.append(acceptance_root / p)
        # Evidence in this tree cites paths with a leading "acceptance/"
        # segment as if the acceptance root itself were called that, e.g.
        # `acceptance/VG-1b/D26-observer-effect.md:76-84`. Strip that one
        # logical prefix and check under the real acceptance root BEFORE
        # falling back to a blind repo-root join -- a stale file that
        # happened to exist at repo_root/acceptance/... would otherwise
        # silently win over the correct, intended target.
        if p.parts and p.parts[0] == "acceptance" and len(p.parts) > 1:
            candidates.append(acceptance_root.joinpath(*p.parts[1:]))
        # Source citations in this evidence tree are frequently written
        # relative to the `dreamy` PACKAGE, not the repo (mirroring
        # `from dreamy.X import Y` imports) -- e.g. `tui/app.py` for
        # `scripts/dreamy/tui/app.py`. Checked before the blind repo-root
        # join for the same reason as the acceptance-prefix candidate
        # above: the more specific, convention-matching guess should win
        # over a coincidental hit elsewhere in the repo tree. Narrowly
        # scoped -- it does NOT recurse into arbitrary subpackages (a
        # citation like `views/runs.py` still will not guess its way to
        # `tui/views/runs.py`; that requires inserting a segment the
        # citation never named).
        candidates.append(repo_root / "scripts" / "dreamy" / p)
        candidates.append(repo_root / p)

    for c in candidates:
        if c.exists():
            return (c, None) if c.is_file() else (None, "not_a_file")

    # Basename-only fallback is a last resort reserved for genuinely bare
    # filenames (a single path component). A multi-segment path that
    # failed every qualified candidate above (repo-relative,
    # evidence-relative, citing-dir-relative, package-relative) has
    # already told us it means something structured; discarding that
    # structure to hunt any file with a matching NAME anywhere in the
    # repo would silently guess at exactly the kind of remap D27 warns
    # against.
    if len(p.parts) != 1:
        return None, "file_not_found"

    basename = p.name
    matches: list[Path] = []
    for dirpath, _dirnames, filenames in _walk(repo_root):
        if basename in filenames:
            matches.append(dirpath / basename)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"ambiguous_basename:{len(matches)}"
    return None, "file_not_found"


def sweep(
    acceptance_root: Path,
    repo_root: Path = REPO,
    only_files: list[Path] | None = None,
) -> dict[str, Any]:
    """Sweep citations under `acceptance_root`.

    `only_files` restricts the scan to an exact list of declared artifacts. The
    current-run gate passes the manifest's hash-verified paths, so undeclared
    files sitting next to them cannot affect the verdict in either direction:
    they neither add failures nor -- via a stray remap table -- seal real ones.
    Sealing is discovered only within the same restricted set for that reason.

    With `only_files` omitted the whole tree is walked, which is what the
    historical-debt measurement uses.
    """
    sealed_anchors: set[str] = set()
    files_scanned = 0
    # Counted by reason rather than by a fixed set of named counters: ANY
    # non-None skip reason skips the scan and lands here. An earlier version
    # enumerated "binary" and "unreadable" explicitly, so a newly added reason
    # ("source_not_citation") fell through and the file was scanned anyway --
    # silently, because the named counters still looked plausible.
    files_skipped: collections.Counter[str] = collections.Counter()
    oversized_lines: list[dict] = []
    # Files that sniffed as scannable but raised OSError once actually
    # opened for the real pass (TOCTOU: deleted, permissions changed, or
    # became unreadable between _sniff() and here). Distinct from
    # files_skipped_unreadable, which is the sniff-time failure.
    scan_errors: list[dict] = []
    total_occurrences = 0

    # Grouping key = (normalized_anchor, outcome_key). outcome_key is either
    # "ok:<resolved absolute path>" for a successful resolution, or
    # "err:<reason>" for a failure. Keying on the resolved path (not just
    # "ok") matters: the same anchor TEXT resolving to two different real
    # files from two different citing directories must NOT collapse into
    # one group, or the report would silently hide the divergent case.
    groups: dict[tuple[str, str], dict] = {}

    if only_files is None:
        scan_source: Any = _iter_scan_files(acceptance_root)
    else:
        # Do not trust the caller, even though the manifest validator already
        # checked containment: this function is importable on its own, and a
        # path escaping the root or a symlink would let an undeclared file
        # influence the verdict.
        #
        # Classification mirrors _iter_scan_files exactly -- same suffix rule,
        # same binary sniff -- so a declared .py is skipped as
        # "source_not_citation" here too, rather than being scanned only
        # because it was named explicitly.
        checked: list[tuple[Path, str | None]] = []
        root_resolved = acceptance_root.resolve()
        originals = [Path(c) for c in only_files]

        # Duplicates are rejected, not silently collapsed. A manifest naming
        # the same artifact twice is malformed, and quietly deduping it would
        # hide the defect while still reporting a clean sweep.
        resolved_seen: dict[Path, Path] = {}
        for original in originals:
            # is_symlink() MUST be tested on the ORIGINAL path. After
            # .resolve() the link has already been followed, so the resolved
            # path is never a symlink and the check would never fire.
            if original.is_symlink():
                raise ValueError(f"declared artifact is a symlink: {original}")
            resolved = original.resolve()
            if resolved in resolved_seen:
                raise ValueError(
                    f"declared artifact listed twice: {original} and "
                    f"{resolved_seen[resolved]} resolve to {resolved}"
                )
            resolved_seen[resolved] = original

        for candidate in sorted(resolved_seen):
            if root_resolved not in candidate.parents:
                raise ValueError(f"declared artifact escapes the root: {candidate}")
            if not candidate.is_file():
                raise ValueError(
                    f"declared artifact is not a regular file: {candidate}"
                )
            if candidate.suffix in _NON_CITATION_SUFFIXES:
                checked.append((candidate, "source_not_citation"))
            else:
                checked.append((candidate, _sniff(candidate)))
        scan_source = iter(checked)

    for f, skip_reason_ in scan_source:
        try:
            rel = str(f.relative_to(acceptance_root))
        except ValueError:
            rel = str(f)

        # Any non-None reason skips. No per-reason branch, so a reason added
        # later cannot fall through to scanning.
        if skip_reason_ is not None:
            files_skipped[skip_reason_] += 1
            continue

        files_scanned += 1
        try:
            if f.suffix == ".md":
                for a in _extract_remap_table_anchors(f):
                    sealed_anchors.add(a)

            for line_no, m, oversized in _scan_file_for_anchors(f):
                if oversized:
                    oversized_lines.append({"file": rel, "line": line_no})
                    continue

                total_occurrences += 1
                norm = _normalize_anchor(m)
                cited_path = m.group(1)
                start_s, end_s = m.group(2), m.group(3)

                resolved_path, reason = _resolve(cited_path, f, acceptance_root, repo_root)
                ok = False
                if resolved_path is not None:
                    lc = _line_count(resolved_path)
                    start = int(start_s)
                    end = int(end_s) if end_s else start
                    if lc is None:
                        reason = "unreadable_target"
                    elif start < 1 or start > lc:
                        reason = f"line_out_of_range (file has {lc} lines)"
                    elif end > lc:
                        reason = f"range_end_out_of_range (file has {lc} lines)"
                    elif end < start:
                        reason = "range_end_before_start"
                    else:
                        ok = True

                outcome_key = f"ok:{resolved_path}" if ok else f"err:{reason}"
                key = (norm, outcome_key)
                entry = groups.setdefault(key, {
                    "anchor": norm,
                    "ok": ok,
                    "reason": reason,
                    "resolved_path": str(resolved_path) if resolved_path else None,
                    "cited_in": set(),
                })
                entry["cited_in"].add(f"{rel}:{line_no}")
        except OSError as e:
            # Reason code + path only -- no raw exception text, which can
            # embed OS-level detail beyond the path/line-number contract
            # this checker holds itself to everywhere else.
            scan_errors.append({"file": rel, "reason": type(e).__name__})
            continue

    resolved = 0
    sealed_documented: list[dict] = []
    unresolved: list[dict] = []

    for (norm, _outcome_key), entry in sorted(groups.items(), key=lambda kv: kv[0]):
        if entry["ok"]:
            resolved += 1
            continue

        record = {
            "anchor": norm,
            "reason": entry["reason"],
            "cited_in": sorted(entry["cited_in"]),
        }
        if norm in sealed_anchors:
            sealed_documented.append(record)
        else:
            unresolved.append(record)

    # A "PASS" verdict must mean "every citation was actually checked and
    # none were broken" -- not "every citation we managed to check was
    # fine". Coverage gaps (files that errored mid-scan, or lines too
    # large to check) gate the verdict too, separately from content
    # findings, so an incomplete sweep cannot silently report success.
    coverage_complete = (
        not files_skipped["unreadable"] and not oversized_lines and not scan_errors
    )
    verdict = "PASS" if (not unresolved and coverage_complete) else "FAIL"
    return {
        "acceptance_root": str(acceptance_root),
        "repo_root": str(repo_root),
        "files_scanned": files_scanned,
        # Legacy keys retained so existing reviewer artifacts and reader code
        # keep working; both are now derived from files_skipped rather than
        # tracked separately, so they cannot drift from it.
        "files_skipped_binary": files_skipped["binary"],
        "files_skipped_unreadable": files_skipped["unreadable"],
        # Authoritative, reason-keyed view. Includes reasons with no legacy
        # key (e.g. "source_not_citation") so an exclusion is always visible
        # in the result rather than only in the code.
        "files_skipped_by_reason": dict(files_skipped),
        "scan_errors": scan_errors,
        "oversized_lines": oversized_lines,
        "total_occurrences": total_occurrences,
        # unique_anchors counts distinct anchor TEXT; resolution_groups
        # counts distinct (anchor text, resolution outcome) pairs. They can
        # differ when the same literal anchor string resolves to different
        # concrete targets from different citing files -- that divergence
        # is deliberately visible rather than folded away, so the two
        # counts are reported separately instead of forced into one number.
        "unique_anchors": len({norm for norm, _ in groups}),
        "resolution_groups": len(groups),
        "resolved": resolved,
        "sealed_documented": sealed_documented,
        "unresolved": unresolved,
        "coverage_complete": coverage_complete,
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    positional = [a for a in argv if a != "--json"]

    if len(positional) > 1:
        print("usage: python3 test_citation_selfcheck.py [acceptance_root] [--json]",
              file=sys.stderr)
        return 2

    root = Path(positional[0]).expanduser() if positional else DEFAULT_ACCEPTANCE_ROOT
    if not root.is_dir():
        print(f"error: acceptance root not found or not a directory: {root}", file=sys.stderr)
        return 2

    result = sweep(root)

    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"acceptance_root: {result['acceptance_root']}")
        print(f"files scanned: {result['files_scanned']}")
        print(f"files skipped (binary): {result['files_skipped_binary']}")
        print(f"files skipped (unreadable): {result['files_skipped_unreadable']}")
        if result["scan_errors"]:
            print(f"scan errors (coverage gap): {len(result['scan_errors'])}")
            for e in result["scan_errors"]:
                print(f"  - {e['file']}: {e['reason']}")
        if result["oversized_lines"]:
            print(f"oversized lines (unscannable, coverage gap): {len(result['oversized_lines'])}")
        print(f"total citation occurrences: {result['total_occurrences']}")
        print(f"unique anchors: {result['unique_anchors']}")
        print(f"resolution groups: {result['resolution_groups']}")
        print(f"resolved: {result['resolved']}")
        print(f"sealed/documented (not a fresh failure): {len(result['sealed_documented'])}")
        for r in result["sealed_documented"]:
            print(f"  - {r['anchor']}  ({r['reason']})  cited in: {', '.join(r['cited_in'])}")
        print(f"unresolved: {len(result['unresolved'])}")
        for r in result["unresolved"]:
            print(f"  - {r['anchor']}  ({r['reason']})  cited in: {', '.join(r['cited_in'])}")
        print(f"verdict: {result['verdict']}")

    return 0 if result["verdict"] == "PASS" else 1


@pytest.mark.skipif(
    not os.environ.get(CURRENT_RUN_MANIFEST_ENV),
    reason=(
        f"no current run declared; set {CURRENT_RUN_MANIFEST_ENV} to a "
        "validated run manifest path"
    ),
)
def test_current_run_citations_resolve():
    """Citations in the CURRENT run must resolve.

    Scoped to one declared, hash-verified run rather than the whole historical
    tree. Sweeping everything can never go green while any past artifact holds a
    dead citation, so the gate gets waived -- and a waived gate proves nothing.
    Historical evidence stays immutable and out of scope; this gate speaks only
    for the run it was given.

    The manifest path must be supplied explicitly. There is deliberately no
    directory fallback and no "latest run" discovery: an empty or wrong
    directory yields zero anchors, zero failures, and a false PASS.
    """
    from dreamy.acceptance import validate_run_manifest

    # No third argument: the default IS the production trust base. Passing one
    # here is what let this gate diverge from production before.
    run = validate_run_manifest(Path(os.environ[CURRENT_RUN_MANIFEST_ENV]), REPO)
    # Exactly the manifest's hash-verified artifacts. Sweeping run.root instead
    # would scan undeclared neighbours, discarding the containment and hash
    # guarantees the validator just established.
    result = sweep(run.root, only_files=sorted(run.artifacts.values()))
    assert result["coverage_complete"], (
        f"run {run.run_id} citation coverage incomplete: "
        f"scan_errors={result['scan_errors']} "
        f"oversized_lines={result['oversized_lines']}"
    )
    assert not result["unresolved"], (
        f"run {run.run_id}: {len(result['unresolved'])} citation(s) unresolved "
        f"and not sealed/documented: {result['unresolved']}"
    )


def _tracked_tree_debt() -> int | None:
    """Unresolved-citation count measured against the tracked tree.

    Returns None when this is not a git checkout. Sweeping from a detached
    worktree of HEAD makes the number environment-independent: a working
    directory carrying gitignored files resolves anchors a clean clone cannot.
    """
    import json as _json
    import shutil
    import subprocess
    import tempfile

    repo = Path(__file__).resolve().parents[2]
    if not (repo / ".git").exists():
        return None

    tmp = Path(tempfile.mkdtemp(prefix="citation-tracked-"))
    worktree = tmp / "wt"
    try:
        add = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "--detach",
             str(worktree), "HEAD"],
            capture_output=True, text=True,
        )
        assert add.returncode == 0, f"could not create worktree: {add.stderr.strip()}"

        script = worktree / "tests/integration/test_citation_selfcheck.py"
        assert script.exists(), "tracked tree has no citation self-check script"

        # `--json` writes a multi-line document and exits non-zero whenever the
        # sweep verdict is FAIL, which is the normal state while debt exists.
        # Parse the payload; do not trust the exit code.
        proc = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True, text=True, cwd=str(worktree),
        )
        assert proc.stdout.strip(), (
            f"tracked-tree sweep produced no output (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:400]}"
        )
        return len(_json.loads(proc.stdout)["unresolved"])
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
        )
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.skipif(not DEFAULT_ACCEPTANCE_ROOT.is_dir(), reason=skip_reason)
def test_historical_citation_debt_is_recorded():
    """The historical tree's citation debt is measured, not asserted away.

    153 anchors are unresolved: absolute machine-specific paths in reviewer
    markdown, drift, anchors left dead by the `scripts/dreamy` -> `src/dreamy`
    rename, and seven `state.json` anchors pointing into gitignored agent state
    (`.campaign/`).
    Those artifacts are immutable historical record under
    `~/.local/share/dreamy/`; they cannot be edited to make a gate pass, and
    regenerating a human reviewer's document is not something a fresh run can
    do.

    So this test records the debt and fails only if it GROWS. Ratcheting down is
    always allowed; silently adding new dead citations is not.

    The count is taken against the TRACKED tree, not the working directory.
    `sweep` resolves relative anchors against CWD, so seven anchors into
    gitignored `.campaign/state.json` resolve in this checkout and are
    `file_not_found` in every clean one -- 146 here, 153 in a `git worktree` of
    the same commit. Baselining the local 146 made the suite fail for every
    fresh contributor; baselining 153 while measuring locally would instead
    leave seven units of silent slack. Measuring the tracked tree removes both:
    the number is the same for everyone, and the ratchet has no give.

    Baseline corrected 153 -> 154 after measuring it directly. The 153 was
    never the tracked-tree count: sweeping a detached worktree of `c651fc9`,
    the commit that introduced the constant, reports 182. Later work drove the
    real figure to 154, so the gate had been failing on the last unit of a
    number that was wrong when written rather than on newly added debt.

    Verified as pre-existing, not introduced: sweeping detached worktrees of
    `HEAD` and of `806f373` (before the redaction, type, and source-state work)
    yields 154 in both, with an identical unresolved set -- zero added, zero
    resolved.

    Baseline held at 154 rather than raised. Untracking 201 orchestration
    files as publish-prep left 20 acceptance-artifact anchors unresolvable
    (`phase-05-p5.md:36`, `plan.md:141`, `wave-context.md:21`, ...), pushing
    the tracked-tree count to 174 -- but a ratchet that moves UP for
    intentional deletion is not a ratchet. Raising it would encode
    deliberately-broken citations as accepted debt and permanently forfeit
    that slack; the number would become a record of what was tolerated rather
    than of what is true. The cited files stay tracked instead.
    """
    count = _tracked_tree_debt()
    if count is None:
        pytest.skip("not a git checkout; cannot measure tracked-tree debt")
    known_debt = 154
    assert count <= known_debt, (
        f"historical citation debt grew from {known_debt} to {count}; "
        "new dead citations were introduced"
    )


def _require_local_corpus_binding(
    acceptance_root: Path = DEFAULT_ACCEPTANCE_ROOT,
    repo_root: Path = REPO,
) -> None:
    """I7 gate: skip unless *acceptance_root* was produced by this workspace.

    Thin pytest adapter over ``corpus_binding_skip_reason`` — the same call
    the binding proof drives against temp corpora.
    """
    from dreamy.acceptance import corpus_binding_skip_reason

    unbound = corpus_binding_skip_reason(acceptance_root, repo_root)
    if unbound is not None:
        pytest.skip(unbound)

@pytest.mark.skipif(not DEFAULT_ACCEPTANCE_ROOT.is_dir(), reason=skip_reason)
def test_local_sweep_does_not_understate_tracked_debt():
    """Pin how much the working tree flatters the local count.

    This does not gate on equality. Seven `state.json` anchors in immutable
    acceptance artifacts under `~/.local/share/dreamy/` point into gitignored
    `.campaign/`; those artifacts are historical record, and editing them to
    make a gate pass is the move the Iron Rule forbids. The divergence is
    permanent and expected: 146 locally, 153 tracked.

    What must not happen is the gap GROWING, or the ratchet in
    `test_historical_citation_debt_is_recorded` silently regaining slack by
    being re-tuned against a working tree. That test now measures the tracked
    tree so it cannot; this one pins the gap so a new locally-resolving
    citation is visible rather than absorbed.

    Two earlier attempts at this guard failed for structural reasons worth
    recording. Scanning `unresolved` is blind by construction -- an anchor that
    resolves locally is absent from that list. Scanning `resolved` is
    impossible: `sweep` increments it as a counter and discards the anchor text.

    I7: only meaningful when the local corpus was produced by THIS checkout.
    A foreign or unmarked corpus is refused (skip) rather than compared — the
    mismatch is noise, and noise in a gate trains the reader to skip the output.
    """
    _require_local_corpus_binding()

    tracked = _tracked_tree_debt()
    if tracked is None:
        pytest.skip("not a git checkout; cannot compare against tracked tree")

    here = len(sweep(DEFAULT_ACCEPTANCE_ROOT)["unresolved"])
    known_gap = 7
    gap = tracked - here

    assert gap <= known_gap, (
        f"local sweep understates tracked debt by {gap} (was {known_gap}): "
        f"{here} here vs {tracked} tracked. A new citation resolves only "
        "because of a gitignored file. Cite a committed path instead."
    )
    assert gap >= 0, (
        f"local sweep reports MORE debt than the tracked tree ({here} vs "
        f"{tracked}); an uncommitted file is introducing dead citations."
    )


if __name__ == "__main__":
    raise SystemExit(main())
