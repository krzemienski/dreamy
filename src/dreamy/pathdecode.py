"""Decode harness project-directory encodings into absolute paths.

Claude Code and OMP/Pi both flatten absolute paths into single directory
names by replacing '/' with '-'. Hyphenated path components (e.g.
``my-hyphenated-project``) make naive ``replace('-', '/')`` wrong.

Strategy
--------
1. Prefer the native ``cwd`` field on session/message records when present.
2. Fall back to filesystem-aware decoding: at each directory level, match
   the longest child name whose encoding is a prefix of the remaining
   token string. This preserves real hyphens inside component names when
   the path still exists on disk.
3. If the filesystem cannot resolve a tail (deleted project), keep the
   unresolved remainder as a single hyphenated leaf rather than guessing
   slash boundaries.

Claude Code extras
------------------
* Absolute paths, leading ``-``.
* A component starting with ``.`` may be encoded either as an empty
  segment (``/.foo`` → ``--foo``) or with a literal dot (``/.foo`` →
  ``-.foo``). Both forms are tried.
* Worktree dirs sometimes embed ``--`` for a ``/.auto-...`` segment.

OMP / Pi extras
---------------
* Home-relative: ``-Desktop-dreamer`` → ``~/Desktop/dreamer``.
* Bare ``-`` → home.
* ``--private-tmp--`` / ``--private-tmp-<name>--`` → under ``/tmp``.
"""
from __future__ import annotations

import re
from pathlib import Path


def _child_encodings(name: str, *, claude_dots: bool) -> list[str]:
    """Return encoded forms of a single path component (with leading '-')."""
    if claude_dots and name.startswith("."):
        # Empty-segment form AND literal-dot form — both observed on disk.
        return ["--" + name[1:], "-" + name]
    return ["-" + name]


def _split_points(body: str) -> list[int]:
    """Candidate end offsets for the next component, LONGEST FIRST.

    A component can only end immediately before a '-' (the encoded separator)
    or at the end of the string. Longest-first is what preserves real hyphens
    inside a component name: `Desktop-my-hyphenated-project` offers
    the full tail before `Desktop-my`, so a directory that genuinely contains
    hyphens wins over a shorter accidental match.
    """
    n = len(body)
    return [i for i in range(n, 0, -1) if i == n or body[i] == "-"]


def _longest_child_match(
    path: Path,
    remaining: str,
    *,
    claude_dots: bool,
) -> tuple[str, str] | None:
    """Return (child_name, rest_of_remaining) for the longest encoding match.

    Probes candidate children by EXISTENCE instead of enumerating `path`.
    This is not a naive hyphen split: every candidate is confirmed on disk
    with `is_dir()`, longest-first, so the hyphen-ambiguity the enumerating
    version solved is still solved — same answer, one stat per candidate
    instead of a full directory listing.

    Enumerating is what made unattended runs hang. `iterdir()` on a
    TCC-protected directory (`~/Desktop`, `~/Documents`) does NOT raise under
    launchd: macOS suspends the call awaiting a consent dialog that can never
    be shown to a background job, so the run blocks forever. Measured: the
    same wheel and database took 17s in the foreground and stalled past 15
    minutes under launchd, blocked in `os.listdir` on `~/Desktop`.
    `except OSError` was retained below
    but was never load-bearing for that case — TCC blocks rather than errors.
    """
    if not remaining or remaining == "-":
        return None
    body = remaining[1:] if remaining.startswith("-") else remaining
    if not body:
        return None

    for end in _split_points(body):
        name, rest = body[:end], body[end:]
        if rest and not rest.startswith("-"):
            continue
        candidates = [name]
        if claude_dots:
            # `/.foo` encodes as `--foo` (empty segment) or `-.foo` (literal
            # dot). After stripping the leading '-', the empty-segment form
            # arrives here as `-foo`, whose real directory name is `.foo`.
            if name.startswith("-"):
                candidates.append("." + name[1:])
            elif not name.startswith("."):
                candidates.append("." + name)
        for cand in candidates:
            if not cand or cand in (".", ".."):
                continue
            try:
                if (path / cand).is_dir():
                    return cand, rest
            except OSError:
                continue
    return None


def _decode_under(
    root: Path,
    remaining: str,
    *,
    claude_dots: bool,
) -> str:
    """Greedy FS walk under *root*. *remaining* must start with '-' or be empty."""
    path = root
    for _ in range(128):
        if not remaining or remaining == "-":
            break
        match = _longest_child_match(path, remaining, claude_dots=claude_dots)
        if match is None:
            break
        child_name, remaining = match
        path = path / child_name
    if remaining and remaining not in ("", "-"):
        # Unresolved tail — keep hyphens (do NOT slash-split).
        leaf = remaining[1:] if remaining.startswith("-") else remaining
        if leaf:
            # The tail is by construction a single filename: the walk consumed
            # every component it could match, and what remains is one
            # unmatched name. Anything else is malformed input.
            #
            # A decoded name becomes the `cwd` of a git subprocess, so `..`
            # here escapes the directory the walk matched — `-tmp-project-..`
            # decoded to `/tmp/project/..` before this check.
            #
            # Raise rather than fall back to the deepest matched ancestor:
            # that would run `git` in a real but WRONG repository and produce
            # plausible evidence attributed to the wrong project. Callers skip
            # attribution instead.
            #
            # Both separators are rejected explicitly. The encoding is POSIX,
            # so a `Path`-based name check would be OS-dependent and would
            # miss a backslash on this platform.
            if leaf in (".", "..") or "/" in leaf or "\\" in leaf:
                raise ValueError(
                    f"refusing to decode malformed directory segment {leaf!r}: "
                    "a traversal or separator component cannot be a filename"
                )
            return str(path / leaf)
    return str(path)


def decode_claude_project_dir(dir_name: str) -> str:
    """Decode a Claude Code ``~/.claude/projects/<dir>`` name to an abs path."""
    if not dir_name or dir_name == "-":
        return "/"
    name = dir_name if dir_name.startswith("-") else "-" + dir_name
    return _decode_under(Path("/"), name, claude_dots=True)


_HOME_FORM = re.compile(r"^home-(?P<slug>.+)-(?P<digest>[0-9a-f]{64})$")


def decode_omp_session_dir(dir_name: str, home: Path | None = None) -> str:
    """Decode an OMP/Pi ``~/.omp/agent/sessions/<dir>`` name to an abs path."""
    if home is None:
        home = Path.home()
    if not dir_name or dir_name == "-":
        return str(home)

    # A slash is NOT accepted here. Splitting a multi-segment name inside the
    # decoder would silently re-admit the traversal shape the unresolved-tail
    # guard exists to reject. Callers that hold a nested path pass only its
    # leading segment (see `_JsonlConnector.scan`).

    # Content-addressed form: `home-<slug>-<64 hex>`. The slug is a flattened
    # basename with its own hyphens intact and the digest is opaque, so the
    # original absolute path is NOT recoverable from this name — a decode
    # would fabricate `~/<slug>-<digest>`, a directory that does not exist.
    # Return empty so the caller falls back to the per-record `cwd`, which is
    # authoritative for this layout.
    if _HOME_FORM.match(dir_name):
        return ""

    # Private-tmp form used for /tmp sandboxes:
    #   --private-tmp--                  → /tmp
    #   --private-tmp-omp-verify-clean-- → /tmp/omp-verify-clean
    if dir_name.startswith("--private-tmp"):
        body = dir_name[len("--private-tmp") :]
        if body.endswith("--"):
            body = body[:-2]
        body = body.lstrip("-")
        if not body:
            return "/tmp"
        return _decode_under(Path("/tmp"), "-" + body, claude_dots=False)

    rel = dir_name[1:] if dir_name.startswith("-") else dir_name
    if not rel:
        return str(home)
    return _decode_under(home, "-" + rel, claude_dots=False)


def _is_unsafe_cwd(candidate: str) -> bool:
    """True when *candidate* must not be handed to a subprocess as `cwd`.

    Lexical only, deliberately. Requiring existence would break the supported
    deleted-project case, where a session names a directory since removed. A
    non-existent path is harmless; one containing `..` is not, because the
    caller's containment assumption no longer holds.
    """
    if not candidate.startswith("/"):
        return True
    if "\x00" in candidate or "\\" in candidate:
        # NUL is rejected by the OS but would surface as an opaque ValueError
        # from subprocess; a backslash is never a POSIX separator, so its
        # presence means the value did not come from a path this code produced.
        return True
    return ".." in candidate.split("/")


def prefer_cwd(record_cwd: str | None, decoded_dir: str) -> str:
    """Prefer a non-empty native cwd field over a directory-name decode.

    `record_cwd` is read straight from a harness transcript, so it is at least
    as attacker-influenced as the encoded directory name — a crafted session
    file can put any string here, and the result becomes the `cwd` of a `git`
    subprocess. Validating only the decoder would leave the wider hole open.

    Returns "" when neither candidate is safe; callers treat that as
    unattributed rather than as a directory to run in.
    """
    if isinstance(record_cwd, str):
        cwd = record_cwd.strip()
        if cwd and not _is_unsafe_cwd(cwd):
            return cwd
    if decoded_dir and _is_unsafe_cwd(decoded_dir):
        return ""
    return decoded_dir
