"""Read-only git evidence gathering for a project path — no mutations, ever."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import logging_util

log = logging_util.get_logger("changes")

_GIT_TIMEOUT = 10
_DOCS_PREFIXES = ("readme", "docs/")


@dataclass
class GitEvidence:
    """Snapshot of read-only git state for a project."""
    status_porcelain: str = ""
    recent_log: list[dict[str, str]] = field(default_factory=list)
    diff_stat: str = ""
    docs_paths_touched: list[str] = field(default_factory=list)
    code_paths_touched: list[str] = field(default_factory=list)
    error: str | None = None


def _run_git(args: list[str], cwd: str) -> str | None:
    """Run a read-only git subcommand; return stdout or None on failure.

    GIT_OPTIONAL_LOCKS=0 is mandatory: without it, plumbing like `git status`
    opportunistically refreshes the index, which writes to .git/ and breaks
    the ADR-004 repository-immutability invariant. Raises FileNotFoundError
    if the git binary itself is missing — caller handles that once at the top
    level rather than per-call.
    """
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        log.warn("git command timed out", args=args, cwd=cwd)
        return None
    if result.returncode != 0:
        log.warn("git command failed", args=args, returncode=result.returncode)
        return None
    return result.stdout


def _classify_path(path: str) -> str:
    lower = path.strip().lower()
    if lower.startswith(_DOCS_PREFIXES):
        return "docs"
    if lower.startswith("readme"):
        return "docs"
    return "code"


def _parse_status_paths(status_porcelain: str) -> list[str]:
    paths: list[str] = []
    for line in status_porcelain.splitlines():
        if not line.strip():
            continue
        # porcelain format: "XY <path>" or "XY <old> -> <new>" for renames
        rest = line[3:] if len(line) > 3 else line.strip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        paths.append(rest.strip())
    return paths


def _parse_diff_stat_paths(diff_stat: str) -> list[str]:
    paths: list[str] = []
    for line in diff_stat.splitlines():
        if "|" not in line:
            continue
        candidate = line.split("|", 1)[0].strip()
        if candidate:
            paths.append(candidate)
    return paths


def _ms_to_iso(since_ms: int) -> str:
    return datetime.fromtimestamp(since_ms / 1000, tz=UTC).isoformat()


def gather_git_evidence(project_path: str, since_ms: int | None = None) -> GitEvidence:
    """Gather read-only git evidence for project_path. Never mutates the repo."""
    evidence = GitEvidence()

    try:
        status_out = _run_git(["status", "--porcelain"], cwd=project_path)
    except FileNotFoundError:
        evidence.error = "git binary not found"
        return evidence

    if status_out is None:
        evidence.error = "git status failed (not a repo or command error)"
        return evidence
    evidence.status_porcelain = status_out

    log_args = ["log", "-n", "30", "--format=%H%x09%s%x09%an%x09%aI"]
    if since_ms is not None:
        log_args.extend(["--since", _ms_to_iso(since_ms)])
    try:
        log_out = _run_git(log_args, cwd=project_path)
    except FileNotFoundError:
        log_out = None
    if log_out:
        for line in log_out.splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            sha, subject, author, date = parts
            evidence.recent_log.append({
                "sha": sha,
                "subject": subject,
                "body": "",
                "author": author,
                "date": date,
            })

    try:
        diff_out = _run_git(["diff", "--stat", "HEAD~10..HEAD"], cwd=project_path)
    except FileNotFoundError:
        diff_out = None
    evidence.diff_stat = diff_out or ""

    touched_paths = set(_parse_status_paths(evidence.status_porcelain))
    touched_paths.update(_parse_diff_stat_paths(evidence.diff_stat))

    for path in sorted(touched_paths):
        if _classify_path(path) == "docs":
            evidence.docs_paths_touched.append(path)
        else:
            evidence.code_paths_touched.append(path)

    return evidence


def docs_drift_signal(code_touched: list[str], docs_touched: list[str]) -> bool:
    """True when code changed but docs didn't — possible documentation drift."""
    return bool(code_touched) and not docs_touched
