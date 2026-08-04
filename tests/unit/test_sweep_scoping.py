"""Exact-artifact scoping for the citation sweep.

The current-run gate must speak only for the run it was handed. Sweeping the
run directory instead of the manifest's declared artifacts would discard the
containment and hash guarantees the manifest validator just established, and
would let an undeclared neighbour change the verdict in either direction:

  * an undeclared dead citation invents a failure the run is not responsible for;
  * an undeclared remap table SEALS a real failure, which is worse — it turns a
    genuine defect into a silent pass.

These tests pin both directions.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integration"
    / "test_citation_selfcheck.py"
)
_spec = importlib.util.spec_from_file_location("_citation_mod", _MODULE_PATH)
assert _spec and _spec.loader
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


@pytest.fixture
def tree(tmp_path):
    """A run directory with a declared artifact and an undeclared neighbour."""
    root = tmp_path / "run-260801-120000-scope"
    root.mkdir()
    (root / "target.md").write_text("# real file\nline two\nline three\n")
    (root / "declared.md").write_text("cites target.md:2\n")
    (root / "undeclared.md").write_text("cites ghost-does-not-exist.md:9\n")
    return root


def test_declared_only_ignores_undeclared_dead_citation(tree):
    """An undeclared neighbour's dead citation must not fail this run."""
    result = cs.sweep(tree, only_files=[tree / "declared.md"])
    assert result["unresolved"] == []
    assert result["files_scanned"] == 1


def test_full_sweep_does_see_the_undeclared_file(tree):
    """Control: without scoping the dead citation IS found.

    Without this the test above could pass because the sweep found nothing at
    all, which would prove nothing.
    """
    result = cs.sweep(tree)
    assert any("ghost-does-not-exist" in u["anchor"] for u in result["unresolved"])


def test_declared_dead_citation_still_fails(tree):
    """Scoping must not become a way to hide a real failure."""
    (tree / "bad.md").write_text("cites missing-file.md:3\n")
    result = cs.sweep(tree, only_files=[tree / "bad.md"])
    assert any("missing-file.md:3" in u["anchor"] for u in result["unresolved"])


def test_duplicate_declared_artifact_rejected(tree):
    """A manifest naming the same artifact twice is malformed.

    Silently deduping would hide the defect while still reporting a clean sweep.
    """
    with pytest.raises(ValueError, match="listed twice"):
        cs.sweep(tree, only_files=[tree / "declared.md", tree / "declared.md"])


def test_duplicate_via_different_paths_rejected(tree):
    """Two spellings of one file are still a duplicate."""
    with pytest.raises(ValueError, match="listed twice"):
        cs.sweep(
            tree,
            only_files=[tree / "declared.md", tree / "." / "declared.md"],
        )


def test_symlink_artifact_rejected(tree):
    """is_symlink() must be tested BEFORE resolve().

    After .resolve() the link is already followed, so the resolved path is
    never a symlink and the check would never fire.
    """
    link = tree / "link.md"
    link.symlink_to(tree / "declared.md")
    with pytest.raises(ValueError, match="is a symlink"):
        cs.sweep(tree, only_files=[link])


def test_artifact_outside_root_rejected(tree, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("cites target.md:1\n")
    with pytest.raises(ValueError, match="escapes the root"):
        cs.sweep(tree, only_files=[outside])


def test_missing_artifact_rejected(tree):
    with pytest.raises(ValueError, match="not a regular file"):
        cs.sweep(tree, only_files=[tree / "nope.md"])


def test_declared_python_file_still_skipped(tree):
    """Naming a .py explicitly must not smuggle it past the source exclusion."""
    py = tree / "helper.py"
    py.write_text('P = "/absolute/ghost.md:99"\n')
    result = cs.sweep(tree, only_files=[py])
    assert result["files_scanned"] == 0
    assert result["files_skipped_by_reason"].get("source_not_citation") == 1
    assert result["unresolved"] == []
