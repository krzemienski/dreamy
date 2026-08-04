"""SAFE-001/SAFE-003: a decoded directory becomes a git subprocess `cwd`.

Found during an independent documentation review, not by a test. Two separate
holes, both reachable from harness data an attacker can influence:

1. `_decode_under`'s unresolved-tail fallback appended the leftover segment
   with no validation. `decode_claude_project_dir("-tmp-project-..")` returned
   `/tmp/project/..`, which `changes.py::gather_git_evidence` then passed
   straight to `subprocess.run(["git", ...], cwd=...)`.
2. `prefer_cwd` returned the native `cwd` field verbatim. That field is read
   from a session transcript, so it is *more* attacker-controlled than the
   encoded directory name.

Design notes worth keeping:

- Validation is **lexical**, not existence-based. A session may legitimately
  name a directory that has since been deleted, and that case must keep
  working. A non-existent path is harmless; a traversing one is not.
- A malformed tail **raises** rather than falling back to the deepest matched
  ancestor. Falling back would run `git` in a real but *wrong* repository and
  produce plausible evidence attributed to the wrong project — worse than a
  visible failure.
"""

from __future__ import annotations

import pytest

from dreamy.pathdecode import (
    decode_claude_project_dir,
    decode_omp_session_dir,
    prefer_cwd,
)


# The prefix must EXIST on disk for the walk to consume it and leave a bare
# `..` tail — that is the state under test. An identity-bearing home path was
# used here originally; `/Users` is the same shape without naming an account.
@pytest.mark.parametrize("encoded", ["-Users-..", "-var-..", "-tmp-.."])
def test_traversal_tail_raises_rather_than_truncating(encoded: str) -> None:
    """A `..` tail must fail loudly, not silently resolve to an ancestor."""
    with pytest.raises(ValueError, match="malformed directory segment"):
        decode_claude_project_dir(encoded)


@pytest.mark.parametrize(
    "encoded",
    [
        "-tmp-project-..",   # one filename literally named "project-.."
        "-tmp-..project",
        "-tmp-a..b",
    ],
)
def test_dots_inside_a_filename_are_not_traversal(encoded: str) -> None:
    """Only a standalone `..` component traverses.

    The encoding preserves hyphens in the unresolved tail, so `project-..` is
    a single legitimate filename. Rejecting it would break real projects whose
    names happen to end in dots — and rejecting more than necessary is how a
    security check gets disabled.
    """
    result = decode_claude_project_dir(encoded)
    assert ".." not in result.split("/"), result


def test_omp_traversal_tail_raises(tmp_path) -> None:
    with pytest.raises(ValueError, match="malformed directory segment"):
        decode_omp_session_dir("-..", home=tmp_path)


def test_ordinary_decode_still_works(tmp_path) -> None:
    """The guard must not break the normal path."""
    (tmp_path / "myproject").mkdir()
    assert decode_omp_session_dir("-myproject", home=tmp_path) == str(
        tmp_path / "myproject"
    )


def test_deleted_project_still_decodes(tmp_path) -> None:
    """Existence is deliberately NOT required.

    A session naming a since-deleted directory is a supported case; requiring
    the path to exist would break it while adding no safety.
    """
    result = decode_omp_session_dir("-gone-forever", home=tmp_path)
    assert result.startswith(str(tmp_path))
    assert ".." not in result


@pytest.mark.parametrize(
    "hostile",
    [
        "/safe/../escape",
        "../relative",
        "relative/path",
        "",
        "/tmp/with\\backslash",
        "/tmp/with\x00nul",
    ],
)
def test_prefer_cwd_rejects_hostile_native_field(hostile: str) -> None:
    """`record_cwd` is read from a transcript and must not be trusted."""
    assert prefer_cwd(hostile, "/safe/fallback") == "/safe/fallback"


def test_prefer_cwd_keeps_a_safe_native_field() -> None:
    assert prefer_cwd("/home/user/project", "/decoded") == "/home/user/project"


def test_prefer_cwd_returns_empty_when_neither_is_safe() -> None:
    """Empty means unattributed — never a directory to run a subprocess in."""
    assert prefer_cwd("../bad", "/also/../bad") == ""


def test_prefer_cwd_falls_back_when_native_is_absent() -> None:
    assert prefer_cwd(None, "/decoded/path") == "/decoded/path"


def test_matched_prefix_leaves_bare_traversal_and_raises(tmp_path) -> None:
    """The layout-dependent case the reviewer demonstrated.

    Whether a `..` reaches the tail depends on the filesystem: the walk
    consumes every component it can match, so `-project-..` leaves `..` alone
    only when a `project` child EXISTS. Without it, the whole string stays one
    literal filename. Both branches are pinned here so a refactor cannot
    silently change which one runs.
    """
    (tmp_path / "project").mkdir()
    with pytest.raises(ValueError, match="malformed directory segment"):
        decode_omp_session_dir("-project-..", home=tmp_path)


def test_without_the_matching_child_it_is_one_literal_name(tmp_path) -> None:
    """Control for the test above — same input, no `project` directory."""
    result = decode_omp_session_dir("-project-..", home=tmp_path)
    assert result == str(tmp_path / "project-..")
    assert ".." not in result.split("/")


def test_deep_match_then_traversal_raises(tmp_path) -> None:
    """Traversal after several matched components is still caught."""
    (tmp_path / "a" / "b").mkdir(parents=True)
    with pytest.raises(ValueError, match="malformed directory segment"):
        decode_omp_session_dir("-a-b-..", home=tmp_path)
