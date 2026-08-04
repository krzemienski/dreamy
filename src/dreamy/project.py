"""Resolve a working-directory path to project identity (git root, remote, branch)."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProjectInfo:
    path: str
    name: str
    git_root: str | None
    git_remote: str | None
    git_branch: str | None


def _run_git(root: Path, *args: str) -> str | None:
    # ADR-004 (zero repo writes). changes.py:38-39 documents GIT_OPTIONAL_LOCKS=0
    # as mandatory for every git call inside a managed repo. The reads here
    # (remote get-url, rev-parse) do not touch the index today, so the invariant
    # held only by argument choice — one added subcommand would break it. Set it
    # here so the guarantee is structural.
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _find_git_root(start: Path) -> Path | None:
    """Walk up from *start* looking for a .git dir or file (worktree)."""
    current = start
    while True:
        candidate = current / ".git"
        if candidate.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def resolve_project(path: str | Path) -> ProjectInfo | None:
    if not path:
        return None

    raw = Path(path).expanduser()
    if raw.exists():
        raw = raw.resolve()

    git_root = _find_git_root(raw)

    if git_root is not None:
        name = git_root.name
        git_remote = _run_git(git_root, "remote", "get-url", "origin")
        git_branch = _run_git(git_root, "rev-parse", "--abbrev-ref", "HEAD")
        root_str: str | None = str(git_root)
    else:
        name = raw.name or str(raw)
        git_remote = None
        git_branch = None
        root_str = None

    return ProjectInfo(
        path=str(raw),
        name=name,
        git_root=root_str,
        git_remote=git_remote,
        git_branch=git_branch,
    )


def normalize_project_path(path: str | Path) -> str:
    resolved = Path(path).expanduser()
    if resolved.exists():
        resolved = resolved.resolve()
    else:
        resolved = Path(str(resolved))
    s = str(resolved)
    if len(s) > 1 and s.endswith("/"):
        s = s.rstrip("/")
    return s


def is_excluded(path: str, include: list[str], exclude: list[str]) -> bool:
    norm = normalize_project_path(path)

    def _under(candidate: str, prefix: str) -> bool:
        p = normalize_project_path(prefix)
        return norm == p or norm.startswith(p.rstrip("/") + "/")

    if exclude:
        for prefix in exclude:
            if _under(norm, prefix):
                return True

    if include:
        for prefix in include:
            if _under(norm, prefix):
                return False
        return True

    return False
