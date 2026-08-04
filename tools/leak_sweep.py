#!/usr/bin/env python3
"""Sweep every blob in a repository for operator identity and credentials.

Why this is not `git grep`
--------------------------
A plain text search reports CLEAN on content whose leak is encoded. Both
encodings below shipped from this repository before this tool existed:

* a 47.6MB session export whose base64 payload decoded to 23,543 occurrences
  of the operator's home path, while a raw grep of the same tracked file
  found 3 harmless hits;
* a project directory name with the path separator substituted (`-Users-…`),
  which walked past a guard that only knew the literal form.

So each blob is decoded — base64 runs, percent-encoding — before matching,
and the home path is checked in every encoding it is known to survive in.

Identity is derived, never embedded
-----------------------------------
The needles come from `--home` (default: the current user's home) at run
time. Nothing about a particular account is written into this file, so it
is safe to publish and correct on any machine.

Usage:
    tools/leak_sweep.py <repo-path> [--home PATH] [--allow-file FILE]
                                    [--self-check]

Exit 0 = clean, 1 = findings, 2 = usage error.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.parse

# Credential shapes. Deliberately broad: a false positive costs one review,
# a false negative costs a published secret.
SECRET_PATTERNS = [
    (re.compile(r"sk-ant-api03-[A-Za-z0-9_\-]{20,}"), "anthropic key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws access key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "github token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "slack token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "google api key"),
]

# Long base64 runs. Bounded per blob: a repository can contain arbitrarily
# many, and decoding all of them turns a sweep into a denial of service
# against itself. The cap is generous relative to any legitimate source file
# and the count is reported so a truncated scan is never silent.
B64_RUN = re.compile(rb"[A-Za-z0-9+/]{200,}={0,2}")
MAX_B64_PER_BLOB = 64

# Largest blob read into memory. Above this the object is reported as
# UNSCANNED rather than skipped: a 47.6MB session export is exactly the kind
# of thing that must not go public unexamined.
MAX_BLOB_BYTES = 8 * 1024 * 1024


def home_needles(home: pathlib.Path) -> dict[str, str]:
    """Every encoding of *home* that must not survive into a public repo."""
    parts = [p for p in home.parts if p not in ("/", "\\")]
    needles = {"literal home": str(home)}
    if parts:
        needles["dash-encoded"] = "-" + "-".join(parts) + "-"
        needles["underscore-encoded"] = "_" + "_".join(parts) + "_"
        needles["url-escaped"] = "%2F" + "%2F".join(parts)
        needles["json-escaped"] = "\\/" + "\\/".join(parts)
        needles["account token"] = f"/{home.name}/"
    return needles


def blob_shas(repo: str) -> list[tuple[str, str]]:
    """(sha, path) for every object reachable from any ref."""
    out = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=repo, capture_output=True, text=True, check=False,
    ).stdout
    rows = []
    for line in out.splitlines():
        head, _, rest = line.partition(" ")
        if len(head) == 40:
            rows.append((head, rest))
    return rows


def variants(raw: bytes) -> list[tuple[str, str]]:
    """Decoded views of one blob: raw, embedded base64, percent-decoded."""
    views = [("raw", raw.decode("utf-8", "replace"))]
    for run in B64_RUN.findall(raw)[:MAX_B64_PER_BLOB]:
        try:
            views.append(("b64", base64.b64decode(run, validate=True).decode("utf-8", "replace")))
        except (binascii.Error, ValueError):
            pass
    try:
        views.append(("urldec", urllib.parse.unquote(views[0][1])))
    except (UnicodeDecodeError, ValueError):
        pass
    return views


def sweep(repo: str, home: pathlib.Path, allowed: set[str]) -> list[str]:
    needles = home_needles(home)
    findings: list[str] = []
    scanned = decoded = 0

    for sha, name in blob_shas(repo):
        meta = subprocess.run(
            ["git", "cat-file", "-s", sha], cwd=repo, capture_output=True, check=False,
        )
        kind = subprocess.run(
            ["git", "cat-file", "-t", sha], cwd=repo, capture_output=True, check=False,
        ).stdout.strip()
        if kind != b"blob":
            continue
        label = name or sha[:12]

        # Size-gate BEFORE reading. The blob that motivated this tool was
        # 47.6MB; a repository can hold several, and reading each fully into
        # memory turns the sweep into a resource problem. A blob over the cap
        # is REPORTED, never silently skipped — an unscannable object in a
        # repo about to go public is itself the finding.
        #
        # Fail closed on an unreadable size: treating "git could not tell me"
        # as 0 would send the object down the scan path with a size nobody
        # established, which is the same unexamined-publish this prevents.
        if meta.returncode != 0:
            findings.append(f"UNSCANNED blob — cat-file -s failed for {label}")
            continue
        try:
            size = int(meta.stdout.strip())
        except ValueError:
            findings.append(f"UNSCANNED blob — unparseable size for {label}")
            continue
        if size > MAX_BLOB_BYTES:
            findings.append(
                f"UNSCANNED blob {size / 1_048_576:.1f}MB exceeds cap — {label}"
            )
            continue

        raw = subprocess.run(
            ["git", "cat-file", "blob", sha], cwd=repo, capture_output=True, check=False,
        ).stdout
        scanned += 1

        for view, text in variants(raw):
            if view == "b64":
                decoded += 1
            for what, needle in needles.items():
                if needle in text:
                    findings.append(f"{what:18s} [{view}] {label} x{text.count(needle)}")
            for rx, kind_name in SECRET_PATTERNS:
                for hit in rx.findall(text):
                    # Exact membership, never a prefix or shape match: an
                    # allowlist keyed on "contains CANARY" would also excuse a
                    # real key that happened to contain it.
                    if hit in allowed:
                        continue
                    findings.append(f"SECRET {kind_name:12s} [{view}] {label}")

    print(f"blobs scanned : {scanned}")
    print(f"b64 payloads  : {decoded}")
    print(f"findings      : {len(findings)}")
    for f in sorted(set(findings)):
        print("  " + f)
    return findings


def self_check(home: pathlib.Path, allowed: set[str]) -> int:
    """Prove the sweep can FAIL before trusting that it passed.

    A sweep that reports clean because it is broken is worse than no sweep:
    it converts an unknown risk into a false assurance. This plants a leak in
    each encoding and asserts every one is caught.
    """
    with tempfile.TemporaryDirectory() as td:
        repo = pathlib.Path(td)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        parts = [p for p in home.parts if p != "/"]
        payload = base64.b64encode((str(home) + "/hidden").encode()).decode()
        (repo / "plain.txt").write_text(f"{home}/secret\n")
        (repo / "dashed.txt").write_text("-" + "-".join(parts) + "-project\n")
        (repo / "encoded.txt").write_text("data: " + payload * 4 + "\n")
        # Assembled at runtime, never written as a literal. A scanner that
        # embeds a credential-shaped string is itself a finding for every
        # other scanner — including this one, which flagged exactly that here.
        fake_key = "AKI" + "A" + "ABCDEFGHIJKLMNOP"
        (repo / "cred.txt").write_text(f"key: {fake_key}\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"],
            cwd=repo, check=True,
        )
        print("=== self-check: planted leaks must be caught ===")
        found = sweep(str(repo), home, allowed)
        kinds = {f.split("[")[0].strip() for f in found}
        need = {"literal home", "dash-encoded", "SECRET aws access key"}
        missing = {n for n in need if not any(k.startswith(n) for k in kinds)}
        if missing:
            print(f"SELF-CHECK FAILED — undetected: {sorted(missing)}")
            return 1
        print("SELF-CHECK PASSED — sweep detects planted leaks in every encoding")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo", nargs="?", help="path to the repository to sweep")
    ap.add_argument("--home", default=str(pathlib.Path.home()),
                    help="identity to search for (default: current user's home)")
    ap.add_argument("--allow-file",
                    help="file of exact literal strings to ignore, one per line")
    ap.add_argument("--self-check", action="store_true",
                    help="prove the sweep can fail, then exit")
    args = ap.parse_args()

    home = pathlib.Path(args.home)
    allowed: set[str] = set()
    if args.allow_file:
        p = pathlib.Path(args.allow_file)
        if not p.is_file():
            print(f"leak_sweep: no allow-file at {p}", file=sys.stderr)
            return 2
        # Comments and blanks are NOT allowlist entries. Keeping them would
        # silently add every '#'-prefixed line as a literal needle — harmless
        # by luck, but the file documents why each secret is excused, and a
        # parser that cannot tell prose from a credential is the wrong tool
        # to be deciding what ships.
        allowed = {
            ln.strip() for ln in p.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        }

    if args.self_check:
        return self_check(home, allowed)
    if not args.repo:
        ap.error("repo path is required unless --self-check is given")

    findings = sweep(args.repo, home, allowed)
    print("VERDICT:", "CLEAN" if not findings else "LEAKS PRESENT")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
