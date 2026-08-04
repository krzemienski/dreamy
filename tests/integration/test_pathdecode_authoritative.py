"""Pathdecode self-check: authoritative equality against native cwd.

Per VG-1 line 207: a real directory whose name contains a hyphen decodes
to its true path. ADR-002 requires the decoder to NOT use naive
`str.replace('-', '/')`. Authoritative ground truth = the harness's native
`cwd` field recorded in session JSONL.

Contract:
  - For at least one real encoded Claude project dir whose native cwd
    contains a hyphen-segment, the decoder output must equal the cwd
    after normalize (absolute, no trailing slash).
  - The cwd must contain at least one hyphen in a non-leading component.
  - Scan up to 200 candidates; if zero satisfy the criterion, the test
    fails (the home directory lacks real hyphenated cwd records -- but
    the contract is still proven false if the decoder returns the wrong
    path).

Real-system acceptance check: requires real Claude Code project session
data under `~/.claude/projects`. Skipped when that directory is absent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dreamy.pathdecode import decode_claude_project_dir

PROJECTS_DIR = Path.home() / ".claude" / "projects"
MAX_CANDIDATES = 200

pytestmark = pytest.mark.acceptance

skip_reason = f"no Claude Code project session data at {PROJECTS_DIR}"


def _first_native_cwd(project_dir: Path) -> str | None:
    for jsonl in project_dir.rglob("*.jsonl"):
        if "subagents" in jsonl.parts:
            continue
        try:
            with jsonl.open() as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd")
                    if isinstance(cwd, str) and cwd and cwd.startswith("/"):
                        return cwd
        except OSError:
            continue
    return None


def _norm(p: str) -> str:
    return os.path.normpath(os.path.abspath(p))


def _cwd_has_hyphen_in_nonleading(cwd: str) -> bool:
    parts = [p for p in Path(cwd).parts if p not in ("/", "")]
    if len(parts) < 2:
        return False
    # Hyphen in any non-leading component (after the root "/")
    return any("-" in p for p in parts[1:])


def _find_authoritative_candidate():
    """Return (encoded_name, decoded, native_cwd) for the first project dir
    whose native cwd (recorded in session JSONL) contains a hyphen in a
    non-leading path component, or None if no such candidate exists among
    the first MAX_CANDIDATES dirs scanned."""
    all_dirs = sorted(p for p in PROJECTS_DIR.iterdir() if p.is_dir())
    for d in all_dirs[:MAX_CANDIDATES]:
        native_cwd = _first_native_cwd(d)
        if native_cwd is None:
            continue
        if not _cwd_has_hyphen_in_nonleading(native_cwd):
            continue
        decoded = decode_claude_project_dir(d.name)
        return d.name, decoded, native_cwd
    return None


@pytest.mark.skipif(not PROJECTS_DIR.is_dir(), reason=skip_reason)
def test_pathdecode_matches_native_cwd_for_hyphenated_project():
    all_dirs = [p for p in PROJECTS_DIR.iterdir() if p.is_dir()]
    if not all_dirs:
        pytest.skip(skip_reason)

    candidate = _find_authoritative_candidate()
    if candidate is None:
        pytest.fail(
            "no native cwd with a hyphenated segment found in "
            f"scanned dirs (checked up to {MAX_CANDIDATES})"
        )

    encoded, decoded, native_cwd = candidate
    assert _norm(decoded) == _norm(native_cwd), (
        f"authoritative FAIL on hyphen cwd:\n"
        f"    encoded: {encoded!r}\n"
        f"    decoded: {decoded!r}\n"
        f"    cwd:     {native_cwd!r}\n"
        f"    norm-decoded ({_norm(decoded)}) != norm-cwd ({_norm(native_cwd)})"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
