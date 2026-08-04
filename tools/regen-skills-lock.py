#!/usr/bin/env python3
"""Regenerate `src/dreamy/resources/skills.lock` from resolved skills [BOOT-002].

A maintainer action, deliberately NOT a `dreamy` subcommand. The product
verifies the lock; it must never rewrite it. A shipped binary that can
regenerate its own pins turns a tamper-evident record into a self-signed one:
"verification failed" would be one command away from "verification passes".

`config/skills.lock` is a symlink to the packaged resource, so writing either
path updates both and they cannot drift.

Usage:
    python3 scripts/regen-skills-lock.py [--check]

    --check   exit 1 if the committed lock differs from what would be
              generated now. Prints a diff summary and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "src" / "dreamy" / "resources" / "skills.lock"

sys.path.insert(0, str(REPO / "src"))

from dreamy import skill_pins  # noqa: E402
from dreamy.prompt_compiler import DEFAULT_CHAINS  # noqa: E402


def _provenance(path: Path, cache: Path) -> dict:
    """Recover installable provenance, or state plainly that none exists.

    A skill under a plugin cache carries a manifest with a version and often a
    repository. A hand-placed skill in the user tree carries nothing — that is
    a real limitation of how it was installed, and the lock records it rather
    than inventing a plausible-looking source.
    """
    try:
        rel = path.relative_to(cache)
    except ValueError:
        return {
            "source_type": "user-skills",
            "source": None,
            "expected_version": None,
            "installable": False,
            "resolution": "manual: no machine-readable origin recorded on disk",
        }

    marketplace, plugin, version = rel.parts[0], rel.parts[1], rel.parts[2]
    manifest = cache / marketplace / plugin / version / ".claude-plugin" / "plugin.json"
    repo = None
    if manifest.exists():
        declared = json.loads(manifest.read_text()).get("repository")
        repo = declared.get("url") if isinstance(declared, dict) else declared
    return {
        "source_type": "plugin",
        "source": repo,
        "expected_version": version,
        "plugin": plugin,
        "marketplace": marketplace,
        "installable": bool(repo),
        "resolution": (
            f"install plugin {plugin}@{version} from {repo}"
            if repo
            else f"plugin '{plugin}' {version} declares no repository"
        ),
    }


def build() -> dict:
    home = Path.home()
    cache = home / ".claude" / "plugins" / "cache"
    constraints = set(DEFAULT_CHAINS.get("_constraints", []))
    names = sorted({n for members in DEFAULT_CHAINS.values() for n in members})

    skills = []
    for name in names:
        body = skill_pins.read_skill_body(name)
        if body is None:
            print(f"  ! {name}: not resolvable, omitted from lock", file=sys.stderr)
            continue
        found = skill_pins._skill_path(name, None)
        entry = {
            "name": name,
            # Safety constraints are non-negotiable. Artifact chains degrade to
            # a missing-skill report, so their absence must not fail a build.
            "required": name in constraints,
            "sha256": skill_pins._sha256_hex(body),
            # Provenance: where this resolved on the generating machine. NEVER
            # an install destination — verification resolves by search root and
            # reports a difference as an observation.
            "observed_absolute_path": str(found.resolve()),
        }
        entry.update(_provenance(found, cache))
        skills.append(entry)

    return {
        "lock_version": 2,
        "description": "Skills used by dreamy prompt chains, pinned by content hash [BOOT-002].",
        "generated_on_host_home": str(home),
        "path_semantics": (
            "'observed_absolute_path' records where each skill resolved on the "
            "generating machine. It is provenance, NOT a destination: "
            "verification resolves by search root and compares, and a mismatch "
            "is reported as an observation, never an error. Portable "
            "installation uses 'source' and 'expected_version'."
        ),
        "search_roots": [
            "~/.claude/skills/<name>/SKILL.md",
            "~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/<name>/SKILL.md",
        ],
        "skills": skills,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed lock is stale; write nothing",
    )
    args = ap.parse_args()

    generated = build()
    text = json.dumps(generated, indent=2) + "\n"

    if args.check:
        if not LOCK.exists():
            print(f"lock absent: {LOCK}", file=sys.stderr)
            return 1
        current = json.loads(LOCK.read_text())
        if current == generated:
            print(f"lock current: {len(generated['skills'])} skills")
            return 0

        # Report WHICH entries moved, not just that something did: "the lock is
        # stale" without the delta sends a maintainer to diff a 200-line file.
        old = {s["name"]: s.get("sha256") for s in current.get("skills", [])}
        new = {s["name"]: s.get("sha256") for s in generated["skills"]}
        for name in sorted(set(old) | set(new)):
            if name not in new:
                print(f"  removed: {name}")
            elif name not in old:
                print(f"  added:   {name}")
            elif old[name] != new[name]:
                print(f"  drifted: {name} {old[name][:12]} -> {new[name][:12]}")
        print("lock is STALE; re-run without --check", file=sys.stderr)
        return 1

    LOCK.write_text(text)
    required = [s["name"] for s in generated["skills"] if s["required"]]
    installable = sum(1 for s in generated["skills"] if s["installable"])
    print(f"wrote {LOCK}")
    print(f"  skills: {len(generated['skills'])} | required: {len(required)}")
    print(f"  installable from a recorded source: {installable}")
    if installable < len(generated["skills"]):
        print(
            "  note: hand-placed skills record no origin; their remediation is manual"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
