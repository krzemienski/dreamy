"""Skill lock manifest — pin required skills by content hash [BOOT-002].

`skill_pins` records what a run *did* use, in the database, after the fact.
That answers reproducibility but not bootstrap: on a pristine machine the
database is empty, so nothing states what *must* be present before the first
run. A missing required skill degraded silently to `missing_on_disk` and the
artifact shipped without it.

This module is the pre-run side. `config/skills.lock` declares the skills a
build requires and the SHA-256 each is expected to have; `verify_lock`
resolves every entry against the real filesystem and reports one of:

    ok        — present, hash matches the pin
    drifted   — present, hash differs (skill edited since pinning)
    unpinned  — present, no expected hash recorded
    missing   — not found in any search root

Required entries that are missing or drifted fail the check. Optional entries
report their state and do not. Every failure carries an exact remediation
string rather than a bare boolean, because "verification failed" without the
path to fix is not actionable on a machine the operator has never seen.

The lock is JSON: it is machine-generated and machine-verified, and JSON is
already this project's write format for state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import skill_pins


class LockError(ValueError):
    """The lock file is absent, unreadable, or structurally invalid."""


# The lock ships INSIDE the package [PKG-002]. A default of
# "config/skills.lock" resolves against the process CWD, so it silently
# succeeds when run from the checkout and silently fails everywhere else —
# the worst available failure mode, because development never sees it.
# `config/skills.lock` is a symlink to this file, so there is one editable
# source and the two cannot drift.
PACKAGED_LOCK = "resources/skills.lock"


def default_lock_path() -> Path:
    """Locate the packaged lock independently of CWD.

    Mirrors `acceptance._load_schema`. Returns a real filesystem path: the
    wheel is not zip-imported, and if that ever changes this raises rather
    than silently falling back to a checkout copy.
    """
    from importlib.resources import as_file, files

    try:
        with as_file(files("dreamy").joinpath(PACKAGED_LOCK)) as p:
            return Path(p)
    except (ModuleNotFoundError, FileNotFoundError, OSError) as exc:
        raise LockError(
            f"packaged skill lock unavailable ({exc}); reinstall dreamy or pass --lock"
        ) from exc


@dataclass(frozen=True)
class SkillStatus:
    name: str
    status: str  # ok | drifted | unpinned | missing
    required: bool
    expected_sha256: str | None
    actual_sha256: str | None
    path: str | None
    installable: bool = False
    observed_absolute_path: str | None = None
    relocated: bool = False
    remediation: str | None = None

    def as_dict(self) -> dict:
        return {
            "skill": self.name,
            "status": self.status,
            "required": self.required,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "path": self.path,
            "installable": self.installable,
            "observed_absolute_path": self.observed_absolute_path,
            "relocated": self.relocated,
            "remediation": self.remediation,
        }


@dataclass
class LockReport:
    entries: list[SkillStatus] = field(default_factory=list)

    @property
    def failures(self) -> list[SkillStatus]:
        """Required entries that are missing or drifted.

        Optional entries never fail: declaring a skill optional is precisely
        the statement that the build tolerates its absence.
        """
        return [
            e for e in self.entries if e.required and e.status in ("missing", "drifted")
        ]

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked": len(self.entries),
            "failed": len(self.failures),
            "skills": [e.as_dict() for e in self.entries],
        }


def load_lock(path: Path) -> list[dict]:
    """Parse and structurally validate a lock file.

    Rejects malformed input rather than skipping bad entries: a lock that
    silently drops an unparsable required skill provides weaker guarantees
    than having no lock at all, while still appearing to pass.
    """
    if not path.exists():
        raise LockError(f"no skill lock at {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise LockError(f"invalid skill lock {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise LockError(f"invalid skill lock {path}: top level must be an object")
    version = raw.get("lock_version")
    if version != 2:
        raise LockError(
            f"unsupported skill lock version {version!r} in {path}; expected 2"
        )
    skills = raw.get("skills")
    if not isinstance(skills, list):
        raise LockError(f"invalid skill lock {path}: 'skills' must be a list")

    for i, entry in enumerate(skills):
        if not isinstance(entry, dict):
            raise LockError(f"invalid skill lock {path}: skills[{i}] must be an object")
        if not isinstance(entry.get("name"), str) or not entry["name"]:
            raise LockError(f"invalid skill lock {path}: skills[{i}] needs a 'name'")
        if "required" in entry and not isinstance(entry["required"], bool):
            raise LockError(
                f"invalid skill lock {path}: skills[{i}].required must be a boolean"
            )
        sha = entry.get("sha256")
        if sha is not None and (not isinstance(sha, str) or len(sha) != 64):
            raise LockError(
                f"invalid skill lock {path}: skills[{i}].sha256 must be a 64-char hex digest"
            )
        if "installable" in entry and not isinstance(entry["installable"], bool):
            raise LockError(
                f"invalid skill lock {path}: skills[{i}].installable must be a boolean"
            )
        # Provenance only — where this skill resolved when the lock was
        # generated. Never an install destination: a path under one
        # developer's home cannot be honoured elsewhere, so `verify_lock`
        # resolves by search root and reports a difference as an observation.
        #
        # Recorded HOME-RELATIVE (`~/...`) rather than absolute. An absolute
        # path bakes the generating user's name into a file that ships in the
        # wheel and, for a public repo, into every clone. Absolute values are
        # still accepted so pre-existing locks keep verifying.
        obs = entry.get("observed_absolute_path")
        if obs is not None:
            if not isinstance(obs, str) or not (obs.startswith("/") or obs.startswith("~/")):
                raise LockError(
                    f"invalid skill lock {path}: skills[{i}].observed_absolute_path "
                    f"must be an absolute or home-relative (~/) path"
                )
        ver = entry.get("expected_version")
        if ver is not None and not isinstance(ver, str):
            raise LockError(
                f"invalid skill lock {path}: skills[{i}].expected_version must be a string"
            )
        # A required entry must be actionable when absent: either installable
        # from a recorded source, or carrying an explicit manual resolution.
        # Without one, "verification failed" is unactionable on a machine the
        # operator has never seen — which is the failure BOOT-002 forbids.
        if entry.get("required", True) and not (
            entry.get("source") or entry.get("resolution")
        ):
            raise LockError(
                f"invalid skill lock {path}: required skill '{entry['name']}' needs "
                f"a 'source' or an explicit 'resolution'"
            )
    return skills


def verify_lock(path: Path, home: Path | None = None) -> LockReport:
    """Resolve every locked skill against the filesystem and report status."""
    report = LockReport()
    for entry in load_lock(path):
        name = entry["name"]
        required = bool(entry.get("required", True))
        expected = entry.get("sha256")

        body = skill_pins.read_skill_body(name, home)
        installable = bool(entry.get("installable", False))
        if body is None:
            # BOOT-002 accepts installation OR an exact remediation. Which one
            # applies is a property of the entry, not a policy choice: a skill
            # whose plugin declares a repository can be installed, and one
            # placed by hand records no origin to install from. Stating the
            # second case plainly beats emitting a command that cannot work.
            resolution = entry.get("resolution")
            if not resolution:
                resolution = (
                    f"install '{name}' from {entry['source']}"
                    if entry.get("source")
                    else f"no machine-readable origin recorded for '{name}'"
                )
            report.entries.append(
                SkillStatus(
                    name=name,
                    status="missing",
                    required=required,
                    expected_sha256=expected,
                    actual_sha256=None,
                    path=None,
                    installable=installable,
                    remediation=(
                        f"{resolution}; then place SKILL.md so it resolves under a "
                        f"documented search root and re-run 'dreamy skills verify'"
                    ),
                )
            )
            continue

        actual = skill_pins._sha256_hex(body)
        found = skill_pins._skill_path(name, home)
        if expected is None:
            status, remediation = "unpinned", (
                f"record sha256 {actual} for '{name}' in the lock to pin it"
            )
        elif actual == expected:
            status, remediation = "ok", None
        else:
            status, remediation = "drifted", (
                f"'{name}' at {found} has sha256 {actual}, lock expects {expected}; "
                f"restore the pinned version or update the lock deliberately"
            )
        # Path relocation is expected on any machine but the generating one,
        # so it is reported rather than failed: content identity is the
        # guarantee, location is provenance.
        #
        # `~` is expanded before comparing. A home-relative provenance value
        # (the portable form, which avoids shipping a username) would
        # otherwise never string-match a resolved absolute path, marking every
        # entry relocated on the very machine that generated the lock.
        obs = entry.get("observed_absolute_path")
        obs_resolved = str(Path(obs).expanduser()) if obs else None
        relocated = bool(obs_resolved) and found is not None and str(found.resolve()) != obs_resolved
        report.entries.append(
            SkillStatus(
                name=name,
                status=status,
                required=required,
                expected_sha256=expected,
                actual_sha256=actual,
                path=str(found) if found else None,
                installable=installable,
                observed_absolute_path=obs,
                relocated=relocated,
                remediation=remediation,
            )
        )
    return report
