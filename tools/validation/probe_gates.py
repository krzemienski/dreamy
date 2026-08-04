#!/usr/bin/env python3
"""Real-system probes for the I6–I10 validation surfaces.

This is NOT a pytest module — it is a probe per the project's standing
Iron-Rule directive (no unit tests under active `.crucible/active`). It
exercises the real code paths and prints observed results. Run it, capture
the output under `evidence/`, and read the exit code:

    tools/validation/probe_gates.py <evidence-output-file>

Covers, against the real system (temp git repos, real Store, real CLI):
  I6  release/leak-sweep gate refusals
  I9  cadence staleness classification
  I10 documented-example CLI acceptance
  I8  Store commit guard

Every section prints PASS/FAIL with what it broke and what it observed — a
probe that cannot fail is no evidence at all.
"""
from __future__ import annotations

import base64
import pathlib
import subprocess
import sys
import tempfile

# Resolve the repo from this file's own location, never an embedded absolute
# path — an absolute home path in a published tool is a disclosure, and the
# release sweep (correctly) refuses to ship one.
REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dreamy.store import Store  # noqa: E402

PY = sys.executable
SWEEP = str(REPO / "tools/leak_sweep.py")
HOME = "/Users/probe-operator"
PARTS = ["Users", "probe-operator"]

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")


def git_repo(path: pathlib.Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    for name, body in files.items():
        (path / name).write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"],
        cwd=path, check=True,
    )


def sweep(path: pathlib.Path) -> tuple[int, str]:
    r = subprocess.run(
        [PY, SWEEP, str(path), "--home", HOME],
        capture_output=True, text=True,
    )
    return r.returncode, r.stdout


print("=== I6: leak-sweep detects each encoding ===")
with tempfile.TemporaryDirectory() as td:
    root = pathlib.Path(td)
    cases = {
        "literal": f"path: {HOME}/work\n",
        "dash-encoded": "dir: -Users-probe-operator-project\n",
        "base64-hidden": "d: " + base64.b64encode(f"{HOME}/hidden".encode() * 15).decode() + "\n",
    }
    for name, body in cases.items():
        repo = root / name
        repo.mkdir()
        git_repo(repo, {"f.txt": body})
        rc, _ = sweep(repo)
        record(f"sweep detects {name}", rc == 1, f"exit={rc} (expected 1)")

print("\n=== I6: clean control ===")
with tempfile.TemporaryDirectory() as td:
    repo = pathlib.Path(td)
    git_repo(repo, {"f.txt": "ordinary prose about software\n"})
    rc, out = sweep(repo)
    record("clean control passes", rc == 0, f"exit={rc} (expected 0)")


print("\n=== I9: cadence classifies stale / ok / insufficient ===")
from dreamy import cadence  # noqa: E402

HOUR = 3600_000
NOW = int(__import__("time").time() * 1000)
with tempfile.TemporaryDirectory() as td:
    store = Store(pathlib.Path(td) / "state.db")
    # 30 records 1h apart, watermark 9h in the past -> 9 gaps > 8 => stale
    series = [NOW - (30 - i) * HOUR for i in range(30)]
    a = cadence.refresh(store, "probe", series, now_ms=NOW - 9 * HOUR)
    assess = cadence.assess(
        "probe", watermark_ms=NOW - 9 * HOUR,
        median_gap_ms=a.median_gap_ms, sample_size=a.sample_size,
        computed_ms=a.computed_ms, now_ms=NOW,
    )
    record("stale source classified stale", assess.stale is True,
           f"status={assess.status} (expected stale)")

    assess_ok = cadence.assess(
        "probe", watermark_ms=NOW - HOUR // 2,
        median_gap_ms=a.median_gap_ms, sample_size=a.sample_size,
        computed_ms=a.computed_ms, now_ms=NOW,
    )
    record("recent source classified ok", assess_ok.stale is False,
           f"status={assess_ok.status} (expected ok)")

    few = cadence.refresh(store, "few", [NOW, NOW - HOUR], now_ms=NOW)
    record("below min-sample is insufficient, never stale",
           few.status == cadence.STATUS_INSUFFICIENT and not few.stale,
           f"status={few.status} stale={few.stale}")
    store.close(discard=True)


print("\n=== I8: Store commit guard ===")
with tempfile.TemporaryDirectory() as td:
    s = Store(pathlib.Path(td) / "g.db")
    s.start_run("r1", NOW)
    try:
        s.close()
        record("close() with pending writes raises", False, "no exception")
    except RuntimeError:
        record("close() with pending writes raises", True, "raised RuntimeError")

    s2 = Store(pathlib.Path(td) / "g2.db")
    s2.start_run("r2", NOW)
    s2.commit()
    try:
        s2.close()
        record("commit()+close() does not raise", True, "closed cleanly")
    except RuntimeError:
        record("commit()+close() does not raise", False, "raised unexpectedly")


print("\n=== I10: documented examples are CLI-accepted ===")
import re

docs = ["README.md", "docs/operations/USAGE.md", "docs/operations/RUNBOOK.md"]
UNSAFE = {"run", "install", "uninstall", "tui", "web", "dismiss", "undismiss", "import", "bundle"}
ran = rejected = skipped = 0
for doc in docs:
    p = REPO / doc
    if not p.exists():
        continue
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\$ (dreamy .+)$", line.strip())
        if not m:
            continue
        parts = m.group(1).split()
        sub = parts[1] if len(parts) > 1 else ""
        if sub in UNSAFE or any(re.fullmatch(r"[A-Z_]{3,}|<.+>", a) for a in parts[2:]):
            skipped += 1
            continue
        r = subprocess.run(
            [sys.executable, "-m"] + parts, capture_output=True, text=True, cwd=str(REPO),
        )
        ran += 1
        if "unrecognized arguments" in r.stderr or "invalid choice" in r.stderr:
            rejected += 1
record("documented examples accepted by CLI", rejected == 0,
       f"ran={ran} rejected={rejected} skipped={skipped}")


print("\n=== I7: corpus binding ===")
from dreamy import acceptance  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    base = pathlib.Path(td)
    corpus = base / "acceptance"
    corpus.mkdir()
    fake_repo = base / "checkout"
    fake_repo.mkdir()

    # Instance id is stable across reads for one workspace.
    id1 = acceptance.workspace_instance_id(fake_repo)
    id2 = acceptance.workspace_instance_id(fake_repo)
    record("workspace instance id is stable across reads", id1 == id2,
           f"id matches: {id1 == id2}")

    # Absent marker -> skip reason (unbound).
    reason_absent = acceptance.corpus_binding_skip_reason(corpus, fake_repo)
    record("absent marker skips", reason_absent is not None,
           f"reason: {'skip' if reason_absent else 'RUN'}")

    # Foreign marker -> skip reason (foreign workspace).
    (corpus / acceptance.PRODUCED_BY_NAME).write_text("a-different-workspace-uuid")
    reason_foreign = acceptance.corpus_binding_skip_reason(corpus, fake_repo)
    record("foreign marker skips", reason_foreign is not None,
           f"reason: {'skip' if reason_foreign else 'RUN'}")

    # Matching marker -> runs (None).
    acceptance.write_produced_by(corpus, fake_repo)
    reason_bound = acceptance.corpus_binding_skip_reason(corpus, fake_repo)
    record("matching marker runs", reason_bound is None,
           f"reason: {'RUN' if reason_bound is None else 'skip'}")


print("\n=== RESULT ===")
failed = [n for n, ok, _ in results if not ok]
print(f"  {len(results) - len(failed)}/{len(results)} probes pass")
if failed:
    print(f"  FAILURES: {failed}")
    sys.exit(1)
print("  ALL PROBES PASS")
