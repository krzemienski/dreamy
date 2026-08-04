"""The skill lock must be reachable from an installed package [PKG-002].

The original default was `config/skills.lock` — resolved against the process
CWD. That is the worst available failure mode: it succeeds from the checkout,
so development never sees it, and fails everywhere else. Measured directly:
from the checkout it passed with 10 skills checked; from a temporary
directory it raised `no skill lock at config/skills.lock`.

`config/skills.lock` is a symlink to `src/dreamy/resources/skills.lock`, so
there is exactly one editable source and the shipped copy cannot drift from
the version-controlled one. `test_config_symlink_points_at_packaged_lock`
fails if someone replaces the symlink with a copy.
"""

from __future__ import annotations

import json
from pathlib import Path

from dreamy.skills_lock import PACKAGED_LOCK, default_lock_path, verify_lock

REPO = Path(__file__).resolve().parents[2]


def test_packaged_lock_resolves():
    """The default must come from the package, not the working directory."""
    p = default_lock_path()
    assert p.exists()
    assert p.name == "skills.lock"
    assert "resources" in p.parts


def test_default_is_not_cwd_relative(tmp_path, monkeypatch):
    """Resolution must not depend on where the process was started.

    Run from an empty directory containing no `config/`: a CWD-relative
    default would raise here.
    """
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "config").exists()

    report = verify_lock(default_lock_path())
    assert len(report.entries) == 10


def test_config_symlink_points_at_packaged_lock():
    """One editable source. A copy would be free to drift; a symlink cannot."""
    cfg = REPO / "config" / "skills.lock"
    assert cfg.is_symlink(), "config/skills.lock must be a symlink, not a copy"
    assert cfg.resolve() == (REPO / "src" / "dreamy" / PACKAGED_LOCK).resolve()


def test_lock_content_identical_through_both_paths():
    """Whichever path a caller uses, the bytes must be the same."""
    packaged = default_lock_path().read_bytes()
    via_config = (REPO / "config" / "skills.lock").read_bytes()
    assert packaged == via_config


def test_lock_records_no_generating_home_in_required_resolution():
    """A required skill's remediation must not hardcode another machine's home.

    The observed path is provenance and may name any machine, but the
    instruction an operator follows has to be portable.
    """
    lock = json.loads(default_lock_path().read_text())
    for entry in lock["skills"]:
        if not entry["required"]:
            continue
        resolution = entry.get("resolution") or ""
        assert "/Users/" not in resolution, f"{entry['name']} resolution is machine-specific"
