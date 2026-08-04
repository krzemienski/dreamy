"""Skill lock verification [BOOT-002].

The interesting cases are the ones that must NOT fail. A verifier that fails
on everything unexpected is useless on a second machine, where relocation is
guaranteed and optional skills are routinely absent. Content identity is the
guarantee; location is provenance.

`test_shipped_lock_verifies` runs against the real `config/skills.lock` and
the real filesystem, so lock drift surfaces here rather than on a user's
first run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dreamy.skills_lock import LockError, verify_lock

REPO = Path(__file__).resolve().parents[2]
SHIPPED = REPO / "config" / "skills.lock"


@pytest.fixture
def lock() -> dict:
    return json.loads(SHIPPED.read_text())


def _write(tmp_path: Path, obj: dict) -> Path:
    p = tmp_path / "skills.lock"
    p.write_text(json.dumps(obj))
    return p


def test_shipped_lock_verifies():
    """The committed lock must pass against the real filesystem."""
    report = verify_lock(SHIPPED)
    assert report.ok, [f.as_dict() for f in report.failures]
    assert len(report.entries) == 10


def test_shipped_lock_declares_contract_fields(lock):
    """Every contract field must be present on every entry.

    The spec names six: skill name, source, installed path, content hash,
    expected version, and required/optional status. Optional-valued fields may
    be null (a hand-placed skill has no source), but the key must exist so a
    consumer never has to guess whether it was omitted or unknown.

    The shipped lock records provenance HOME-RELATIVE (`~/...`). An absolute
    path would bake the generating user's name into a file that ships in the
    wheel and, in a public repo, into every clone. Absolute values remain
    valid so pre-existing locks keep verifying.
    """
    required_keys = {
        "name", "source", "observed_absolute_path",
        "sha256", "expected_version", "required",
    }
    for entry in lock["skills"]:
        assert required_keys <= set(entry), f"{entry['name']} missing {required_keys - set(entry)}"
        observed = entry["observed_absolute_path"]
        assert observed.startswith("~/"), (
            f"{entry['name']} ships a non-portable path: {observed}"
        )
        assert "/Users/" not in observed, "no username may ship in the lock"
        assert len(entry["sha256"]) == 64


def _with_required(lock: dict, name: str | None = None) -> dict:
    """Return a copy of *lock* with one entry marked required.

    The shipped lock has no required entry: every candidate was unobtainable
    on a clean machine (`installable: false`, `source: null`), so marking it
    required made `pip install dreamy && dreamy skills verify` fail by
    construction — a packaging defect, not a real dependency.

    These tests cover the required-skill MECHANISM, which must keep working
    for whenever an obtainable required skill is added. Binding them to
    whatever the inventory currently happens to contain conflates "does the
    gate work" with "is anything required today"; they are separate questions
    and only the first belongs here.
    """
    import copy
    out = copy.deepcopy(lock)
    target = out["skills"][0] if name is None else next(
        s for s in out["skills"] if s["name"] == name
    )
    target["required"] = True
    return out


def test_missing_required_skill_fails_with_remediation(tmp_path, lock):
    """A required skill that cannot be found must fail AND say what to do."""
    empty_home = tmp_path / "home"
    (empty_home / ".claude" / "skills").mkdir(parents=True)

    report = verify_lock(_write(tmp_path, _with_required(lock)), home=empty_home)
    assert not report.ok
    for failure in report.failures:
        assert failure.status == "missing"
        assert failure.remediation, f"{failure.name} failed without remediation"
        assert "dreamy skills verify" in failure.remediation


def test_shipped_lock_requires_only_obtainable_skills(lock):
    """`required` may not be set on a skill a clean machine cannot obtain.

    This is the invariant whose violation broke `dreamy skills verify` after
    a plain `pip install`: three constraint skills were required, unobtainable,
    and verified as missing on every machine but the one that wrote the lock.
    """
    for entry in lock["skills"]:
        if entry.get("required"):
            assert entry.get("installable"), (
                f"{entry['name']} is required but not installable; a clean "
                "machine can never satisfy it"
            )
            assert entry.get("source"), (
                f"{entry['name']} is required but declares no source"
            )


def test_drifted_required_skill_fails(tmp_path, lock):
    lock = _with_required(lock)
    idx = next(i for i, s in enumerate(lock["skills"]) if s["required"])
    lock["skills"][idx]["sha256"] = "0" * 64

    report = verify_lock(_write(tmp_path, lock))
    assert not report.ok
    assert [f.name for f in report.failures] == [lock["skills"][idx]["name"]]


def _plant(home: Path, name: str, body: str) -> Path:
    """Create a real SKILL.md under a temp home's user-skills search root.

    `drifted` and `relocated` are only reachable when the file EXISTS and its
    hash differs; a missing file short-circuits to `missing`. Reading whatever
    the operator happens to have installed made both tests pass on this
    workstation and fail on every clean runner — they asserted a status the
    environment, not the code, decided.
    """
    p = home / ".claude" / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_drifted_optional_skill_does_not_fail(tmp_path, lock):
    """Declaring a skill optional IS the statement that its absence is tolerable."""
    idx = next(i for i, s in enumerate(lock["skills"]) if not s["required"])
    name = lock["skills"][idx]["name"]
    lock["skills"][idx]["sha256"] = "1" * 64

    home = tmp_path / "home"
    _plant(home, name, "planted body whose hash cannot match the lock\n")

    report = verify_lock(_write(tmp_path, lock), home=home)
    assert report.ok
    drifted = next(e for e in report.entries if e.name == name)
    assert drifted.status == "drifted"


def test_relocation_is_observed_not_failed(tmp_path, lock):
    """A path recorded on another machine must not fail verification here.

    This is the whole reason the path is provenance rather than a destination:
    on any machine but the generating one it is expected to differ.
    """
    entry = lock["skills"][0]
    entry["observed_absolute_path"] = "/elsewhere/machine/SKILL.md"

    body = "relocated body\n"
    import hashlib
    entry["sha256"] = hashlib.sha256(body.encode()).hexdigest()

    home = tmp_path / "home"
    _plant(home, entry["name"], body)

    report = verify_lock(_write(tmp_path, lock), home=home)
    assert report.ok
    assert report.entries[0].relocated is True
    assert report.entries[0].status == "ok"


def test_unsupported_version_rejected(tmp_path, lock):
    lock["lock_version"] = 1
    with pytest.raises(LockError, match="unsupported skill lock version"):
        verify_lock(_write(tmp_path, lock))


def test_relative_path_rejected(tmp_path, lock):
    """A bare relative path is a malformed record.

    The contract accepts absolute (`/...`) or home-relative (`~/...`) only.
    Anything else has no fixed meaning: it would resolve against whatever
    directory the process happened to start in.
    """
    lock["skills"][0]["observed_absolute_path"] = "relative/path/SKILL.md"
    with pytest.raises(LockError, match="must be an absolute or home-relative"):
        verify_lock(_write(tmp_path, lock))


def test_home_relative_path_accepted(tmp_path, lock):
    """The portable form must verify — it is what the shipped lock uses."""
    lock["skills"][0]["observed_absolute_path"] = "~/.claude/skills/x/SKILL.md"
    report = verify_lock(_write(tmp_path, lock))
    assert report is not None


def test_required_entry_without_resolution_rejected(tmp_path, lock):
    """BOOT-002 demands actionability: install it, or say exactly how.

    A required entry with neither is unactionable on a machine the operator
    has never seen, which is precisely the failure the requirement forbids.
    """
    lock = _with_required(lock)
    idx = next(i for i, s in enumerate(lock["skills"]) if s["required"])
    lock["skills"][idx].pop("source", None)
    lock["skills"][idx].pop("resolution", None)

    with pytest.raises(LockError, match="needs a 'source' or an explicit 'resolution'"):
        verify_lock(_write(tmp_path, lock))


def test_missing_lock_file_is_clear(tmp_path):
    with pytest.raises(LockError, match="no skill lock at"):
        verify_lock(tmp_path / "absent.lock")
