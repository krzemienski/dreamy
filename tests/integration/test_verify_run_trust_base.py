"""The acceptance-runs trust base must not be relocatable by the caller.

`dreamy verify-run` deliberately exposes no `--runs-base` flag: the party
running the validator is the party whose claim is being checked, so letting it
name the trust boundary would defeat the containment check.

That reasoning was right and the implementation incomplete. The base was built
with `expanduser()`, which consults `$HOME`, so the same relocation was
available through the environment. One manifest, identical bytes, was rejected
under the real HOME and ADMITTED under a temporary one.

Two doors had to close:

1. the base itself now resolves through `pwd.getpwuid(os.getuid())`, which
   reads the account database rather than the environment;
2. a manifest `root` is required to be absolute rather than expanded, since
   `root: "~/..."` would otherwise re-enter through the first door.

These run the REAL CLI in a subprocess with a forged `HOME`. An in-process
monkeypatch could not reproduce the defect: the base is computed at import,
and the CLI imports it lazily inside the caller-launched process.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "dreamy"]
RUN_ID = "run-260801-000000-trustprobe"
ARTIFACT_KINDS = (
    "install_checks",
    "pkg001_layout",
    "pkg002_independence",
    "pkg003_metadata",
    "suite_result",
    "wheel_build",
)
REQUIREMENT_IDS = ["PKG-001", "PKG-002", "PKG-003"]


def _head_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def forged_home(tmp_path):
    """A fake home containing a fully valid run — everything but the base."""
    home = tmp_path / "attacker-home"
    run_dir = home / ".local/share/dreamy/acceptance-runs" / RUN_ID
    run_dir.mkdir(parents=True)

    artifacts = {}
    for kind in ARTIFACT_KINDS:
        path = run_dir / f"{kind}.md"
        path.write_text(f"# {kind}\nrun_id: {RUN_ID}\n{' '.join(REQUIREMENT_IDS)}\n")
        artifacts[kind] = {
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    review = run_dir / "review.json"
    review.write_text(
        json.dumps(
            {
                "verdict": "PASS",
                "run_id": RUN_ID,
                "requirement_ids": REQUIREMENT_IDS,
                "reviewer": "reviewer-agent",
            }
        )
    )
    artifacts["review"] = {
        "path": review.name,
        "sha256": hashlib.sha256(review.read_bytes()).hexdigest(),
    }

    def write_manifest(root_value: str) -> Path:
        manifest = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "gate": "G2",
            "commit": _head_sha(),
            "generated_utc": "2026-08-01T00:00:00Z",
            "root": root_value,
            "artifacts": artifacts,
            "requirement_ids": REQUIREMENT_IDS,
            "independent_review": {
                "verdict": "PASS",
                "artifact": "review",
                "reviewer": "reviewer-agent",
            },
            "producer": "producer-agent",
        }
        path = run_dir / "manifest.json"
        path.write_text(json.dumps(manifest))
        return path

    return home, run_dir, write_manifest


def _verify(manifest: Path, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*CLI, "verify-run", str(manifest), "--repo-root", str(REPO_ROOT)],
        env={**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO_ROOT / "src")},
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_forged_home_cannot_relocate_the_base(forged_home) -> None:
    """The original defect: absolute root inside an attacker-controlled home."""
    home, run_dir, write_manifest = forged_home
    manifest = write_manifest(str(run_dir))

    result = _verify(manifest, home)

    assert result.returncode == 2, (
        f"forged HOME was ADMITTED (exit {result.returncode}): {result.stdout}"
    )
    assert "escapes the acceptance-runs base" in result.stderr


def test_tilde_root_cannot_relocate_the_base(forged_home) -> None:
    """The second door: `~` in the manifest expands through the same `$HOME`."""
    home, _run_dir, write_manifest = forged_home
    manifest = write_manifest(f"~/.local/share/dreamy/acceptance-runs/{RUN_ID}")

    result = _verify(manifest, home)

    assert result.returncode == 2, (
        f"tilde root was ADMITTED (exit {result.returncode}): {result.stdout}"
    )
    assert "absolute path" in result.stderr


def test_relative_root_is_refused_not_interpreted(forged_home) -> None:
    """A relative root would resolve against the caller's cwd."""
    home, _run_dir, write_manifest = forged_home
    manifest = write_manifest("acceptance-runs/" + RUN_ID)

    result = _verify(manifest, home)

    assert result.returncode == 2
    assert "absolute path" in result.stderr


def test_verify_run_exposes_no_runs_base_flag() -> None:
    """If the flag ever appears, the environment fix is moot."""
    result = subprocess.run(
        [*CLI, "verify-run", "--help"],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--runs-base" not in result.stdout


def test_citation_gate_uses_the_production_base(forged_home) -> None:
    """The same relocation must be closed on the citation-gate path.

    That gate calls `validate_run_manifest` directly rather than through the
    CLI, and previously passed its own `expanduser()`-derived base — so a CLI-
    only test would have proved nothing about it. Driven here as a subprocess
    with a forged HOME, exactly as the gate runs in acceptance.
    """
    home, run_dir, write_manifest = forged_home
    manifest = write_manifest(str(run_dir))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/test_citation_selfcheck.py::test_current_run_citations_resolve",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        env={
            **os.environ,
            "HOME": str(home),
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "DREAMY_CURRENT_RUN_MANIFEST": str(manifest),
            "DREAMY_RUN_ACCEPTANCE": "1",
        },
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    combined = result.stdout + result.stderr
    # The gate must NOT report a green citation sweep for a run that only
    # exists under the forged home. Either it refuses the manifest or it
    # skips; what it must never do is pass.
    assert "escapes the acceptance-runs base" in combined or result.returncode != 0, (
        f"citation gate accepted a forged-HOME run:\n{combined[:800]}"
    )
