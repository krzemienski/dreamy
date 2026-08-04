"""The validator must have a production consumer.

R-0: `validate_run_manifest` previously had ZERO importers under `src/`. Its
only callers were tests, and the one gate test skips unless an env var is set,
so a fully green suite was compatible with the validator never running. These
tests exercise the CLI path an operator actually invokes.
"""

from __future__ import annotations

import json

from dreamy.cli import _parser, main


def test_verify_run_rejects_manifest_citing_failing_review(tmp_path, capsys):
    """Exit 2, and the reason reaches stderr."""
    run = tmp_path / "acceptance-runs" / "run-260801-120000-fixture"
    run.mkdir(parents=True)
    (run / "review.json").write_text(json.dumps({"verdict": "FAIL"}))
    manifest = run / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "run_id": "x"}))

    assert main(["verify-run", str(manifest), "--repo-root", str(tmp_path)]) == 2
    assert "REJECTED" in capsys.readouterr().err


def test_verify_run_is_reachable_from_the_installed_cli():
    """The subcommand exists in the parser an installed `dreamy` builds."""
    names = {
        name
        for action in _parser()._actions
        for name in (getattr(action, "choices", None) or {})
    }
    assert "verify-run" in names


def test_validator_has_a_production_importer():
    """Guards the R-0 regression directly.

    The check is on `src/`, not on tests: test-only callers are exactly the
    situation that made the validator dead code in production.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "dreamy"
    importers = [
        p.name
        for p in src.rglob("*.py")
        if p.name != "acceptance.py" and "acceptance import" in p.read_text()
    ]
    assert importers, "no production module imports the acceptance validator"
