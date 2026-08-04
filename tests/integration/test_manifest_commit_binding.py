"""The manifest's `commit` and `generated_utc` must actually be validated.

Both are schema-required, and the schema constrains `commit` to a full 40-hex
SHA — but the validator read neither field. Three manifests were ADMITTED with
every artifact hash verified: one omitting `commit`, one omitting
`generated_utc`, and one carrying `commit: "xyz"`.

`commit` is the only binding between an evidence run and the source state it
tested. Without it a manifest is unfalsifiable: the artifacts prove something
ran, but nothing says against what.

What the check does and does not establish:

- It requires the SHA to EXIST as a commit object in the repository, not to
  equal HEAD. Evidence is produced at one commit and validated later, so
  requiring equality would reject every historical run the moment anything else
  was committed.
- It does NOT prove the recorded commit produced the artifacts, or that it
  matches the working tree. Nothing short of signed provenance could. It
  rejects absent, malformed, and fabricated SHAs, and makes the claim auditable
  by someone who can check that commit out.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from dreamy.acceptance import ManifestError, validate_run_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = "G2"
REQUIREMENT_IDS = ["PKG-001", "PKG-002", "PKG-003"]
ARTIFACT_KINDS = (
    "install_checks",
    "pkg001_layout",
    "pkg002_independence",
    "pkg003_metadata",
    "suite_result",
    "wheel_build",
)


def _head_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def manifest_factory(tmp_path):
    """Build a genuinely valid manifest, so each case varies ONE field.

    Everything else must pass on its own merits — otherwise a case could
    "reject" for an unrelated reason and read as a passing test.
    """
    run_id = "run-260801-000000-probe"
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)

    artifacts = {}
    for kind in ARTIFACT_KINDS:
        path = run_dir / f"{kind}.md"
        path.write_text(f"# {kind}\nrun_id: {run_id}\n{' '.join(REQUIREMENT_IDS)}\n")
        artifacts[kind] = {
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    review = run_dir / "review.json"
    review.write_text(
        json.dumps(
            {
                "verdict": "PASS",
                "run_id": run_id,
                "requirement_ids": REQUIREMENT_IDS,
                "reviewer": "probe-reviewer",
            }
        )
    )
    artifacts["review"] = {
        "path": review.name,
        "sha256": hashlib.sha256(review.read_bytes()).hexdigest(),
    }

    def build(**overrides):
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "gate": GATE,
            "commit": _head_sha(),
            "generated_utc": "2026-08-01T00:00:00Z",
            "root": str(run_dir),
            "artifacts": artifacts,
            "requirement_ids": REQUIREMENT_IDS,
            "independent_review": {
                "verdict": "PASS",
                "artifact": "review",
                "reviewer": "probe-reviewer",
            },
            "producer": "probe-producer",
        }
        manifest.update(overrides)
        for key, value in overrides.items():
            if value is None:
                manifest.pop(key, None)
        path = run_dir / "manifest.json"
        path.write_text(json.dumps(manifest))
        return path, tmp_path

    return build


def test_valid_manifest_is_admitted(manifest_factory) -> None:
    """The control. Without it, every rejection below could be spurious."""
    path, base = manifest_factory()
    validated = validate_run_manifest(path, REPO_ROOT, runs_base=base)
    assert validated.verified_hashes == len(ARTIFACT_KINDS) + 1


@pytest.mark.parametrize(
    ("label", "override"),
    [
        ("commit absent", {"commit": None}),
        ("commit not a SHA", {"commit": "xyz"}),
        ("commit abbreviated", {"commit": "7a67b08"}),
        ("commit uppercase hex", {"commit": "A" * 40}),
        ("commit well-formed but fabricated", {"commit": "b" * 40}),
    ],
)
def test_bad_commit_is_rejected(manifest_factory, label, override) -> None:
    path, base = manifest_factory(**override)
    with pytest.raises(ManifestError, match="commit"):
        validate_run_manifest(path, REPO_ROOT, runs_base=base)


@pytest.mark.parametrize(
    ("label", "override"),
    [
        ("absent", {"generated_utc": None}),
        ("unparseable", {"generated_utc": "not-a-date"}),
        ("naive — ambiguous across machines", {"generated_utc": "2026-08-01T00:00:00"}),
        ("non-UTC offset", {"generated_utc": "2026-08-01T00:00:00+05:00"}),
        ("empty", {"generated_utc": "   "}),
    ],
)
def test_bad_generated_utc_is_rejected(manifest_factory, label, override) -> None:
    path, base = manifest_factory(**override)
    with pytest.raises(ManifestError, match="generated_utc"):
        validate_run_manifest(path, REPO_ROOT, runs_base=base)


def test_historical_commit_is_admitted(manifest_factory) -> None:
    """Existence, not equality with HEAD.

    Evidence produced against an earlier commit must remain validatable; that
    is the entire point of recording which commit it was.
    """
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if parent.returncode != 0:
        pytest.skip("no parent commit in this repository")

    path, base = manifest_factory(commit=parent.stdout.strip())
    validated = validate_run_manifest(path, REPO_ROOT, runs_base=base)
    assert validated.run_id == "run-260801-000000-probe"


def test_utc_offset_zero_spelling_is_admitted(manifest_factory) -> None:
    """`+00:00` and `Z` name the same instant; both must pass."""
    path, base = manifest_factory(generated_utc="2026-08-01T00:00:00+00:00")
    validate_run_manifest(path, REPO_ROOT, runs_base=base)
