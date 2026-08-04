"""Skill pin/inline module — lock skill bodies by SHA-256 for byte-identical re-emit.

Q11 (resolved) is pin: the compiler stores SHA-256 of each inlined skill body on
the artifact and warns when the skill file drifts. This module is the read
side: it locates a skill on disk, hashes it, and persists the pin to the
``skill_pins`` table via ``Store.upsert_skill_pin``.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def _skill_path(skill_name: str, home: Path | None) -> Path | None:
    """Locate a skill's SKILL.md.

    Skills live in two places: the user tree (`~/.claude/skills/<name>/`) and
    plugin caches (`~/.claude/plugins/cache/<marketplace>/<plugin>/<ver>/skills/<name>/`).
    Searching only the former silently emitted `missing="true"` for every
    plugin-provided skill, so a prompt declared a chain member it never
    supplied. Plugin hits are sorted for deterministic selection (N6).
    """
    root = home or Path.home()
    direct = root / ".claude" / "skills" / skill_name / "SKILL.md"
    if direct.exists():
        return direct
    cache = root / ".claude" / "plugins" / "cache"
    if cache.is_dir():
        hits = sorted(cache.glob(f"*/*/*/skills/{skill_name}/SKILL.md"))
        if hits:
            return hits[0]
    return None


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_skill_body(skill_name: str, home: Path | None = None) -> str | None:
    """Return the raw SKILL.md body for inline compiler emit, or None if missing."""
    p = _skill_path(skill_name, home)
    if p is None:
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def pin_skill(store, skill_name: str, home: Path | None = None) -> str | None:
    """Read the skill body, SHA-256 it, persist via store.upsert_skill_pin."""

    body = read_skill_body(skill_name, home)
    if body is None:
        return None
    sha = _sha256_hex(body)
    p = _skill_path(skill_name, home)
    store.upsert_skill_pin(name=skill_name, sha=sha, path=str(p) if p else None)
    return sha


def pin_all_used(store, names, home: Path | None = None) -> dict:
    """Bulk variant. Missing skills are omitted from the result; no exception."""

    out: dict[str, str] = {}
    for name in names:
        sha = pin_skill(store, name, home)
        if sha is not None:
            out[name] = sha
    return out


def read_skill_pin_record(store, skill_name: str) -> dict | None:
    """Return the stored pin row as a dict, or None if never pinned."""

    row = store.conn.execute(
        "SELECT skill_name, content_sha256, pinned_ms, path FROM skill_pins WHERE skill_name=?",
        (skill_name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "skill_name": row["skill_name"],
        "content_sha256": row["content_sha256"],
        "pinned_ms": row["pinned_ms"],
        "path": row["path"],
    }


def drift_check(store, skill_name: str, current_sha: str) -> bool:
    """True if the pinned sha differs from the current body sha."""

    rec = read_skill_pin_record(store, skill_name)
    if rec is None:
        return True
    return rec["content_sha256"] != current_sha
