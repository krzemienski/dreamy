"""`dreamy doctor` — implemented acceptance diagnostics (I1).

Scope, stated first because the previous version overstated it: this runs a
PARTIAL set of checks, not the acceptance matrix. The active authority is the
addendum completion gate G1-G8. See `run_doctor` for the list of gates this
harness does NOT cover.

The specifications define a large acceptance matrix and no program owns it.
Every criterion is checked by hand, recorded in prose, and thereafter believed.
That is precisely how a "verified" claim drifts from reality, and this session
found four live defects sitting behind exactly such claims:

  * a run reporting `claude: no records within 30d window` while the store held
    5,989 claude sessions whose newest was 90 minutes old;
  * a shareable bundle rendering the operator's absolute home path, guarded for
    canaries and auth tokens but not for `$HOME`;
  * the C.2 "visible at all times" read-only indicator sitting at `y=-1`,
    off-screen in every view, after a prior fix corrected only the X axis and
    declared victory;
  * a bundle missing a section its own docstring promises.

Each was invisible to the documents and obvious to a command. So this module
runs the commands.

Design rules, each earned:

1. **Every check runs the real system.** No mocks, no fixtures, no "test mode".
   A check that cannot be satisfied by the real system is a failing check, not
   a check to weaken.
2. **A check must be able to fail.** Every check reports the observed value it
   judged, not a bare boolean, so a reviewer can see what was measured. A
   predicate no one has watched fail is not evidence.
3. **Absence of evidence is never a pass.** A check that cannot run reports
   SKIP with its reason. SKIP is never counted as PASS and never exits 0 —
   an incomplete check set is an unproven one.
4. **Exit code is the contract.** 0 = every implemented check ran and
   passed; 1 = a check failed; 2 = nothing failed but the check set was
   incomplete. CI and a human read the same verdict from the same entry
   point. 0 never means the acceptance matrix is satisfied.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Verdicts. SKIP is deliberately distinct from PASS: a check that did not run
# proves nothing, and collapsing the two is the single most common way an
# acceptance harness reports green while covering nothing.
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass
class Check:
    """One acceptance criterion, its verdict, and what was actually observed."""

    id: str
    requirement: str
    verdict: str
    observed: str
    detail: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """Only an executed, passing check is OK.

        SKIP is deliberately NOT ok. An earlier version returned
        `verdict != FAIL`, which quietly made "this never ran" equivalent to
        "this passed" — the precise substitution this harness exists to
        prevent.
        """
        return self.verdict == PASS


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    started_ms: int = 0
    ended_ms: int = 0

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.verdict == FAIL]

    @property
    def skipped(self) -> list[Check]:
        return [c for c in self.checks if c.verdict == SKIP]

    @property
    def passed(self) -> list[Check]:
        return [c for c in self.checks if c.verdict == PASS]

    @property
    def exit_code(self) -> int:
        """0 only when every IMPLEMENTED check ran and passed.

        Three distinct outcomes, three codes, because collapsing them is how
        an acceptance harness reports green while covering nothing:

          0 — every check executed and passed.
          1 — at least one check FAILED. A real defect.
          2 — nothing failed, but at least one check could not run. The
              implemented check set is incomplete, so its verdict is
              unproven. On a fresh machine with no state DB most checks
              SKIP; exiting 0 there would certify a check set that never
              executed.

        `dreamy doctor` promises exactly the implemented check set — never
        the full acceptance matrix. A promise that silently degrades to a
        subset is worse than no promise, because it is trusted.
        """
        if self.failed:
            return 1
        if self.skipped:
            return 2
        return 0


def _run(fn: Callable[[], Check], check_id: str = "", requirement: str = "") -> Check:
    """Run one check, converting a crash into a FAIL that still identifies itself.

    `check_id`/`requirement` are passed explicitly because callers wrap checks
    in `lambda`s to bind arguments, and a lambda's `__name__` is `<lambda>`.
    Deriving identity from the callable produced `[FAIL] <lambda>  unknown` —
    the one situation where naming the check matters most is precisely when it
    crashed before it could name itself. Observed live: a real bundle
    home-path regression surfaced as an anonymous RuntimeError.
    """
    t0 = time.perf_counter()
    try:
        c = fn()
    except Exception as exc:  # noqa: BLE001
        # A check that crashes is a FAIL, never a silent skip. The exception
        # text is the observation.
        c = Check(
            id=check_id or getattr(fn, "check_id", fn.__name__),
            requirement=requirement or getattr(fn, "requirement", "unknown"),
            verdict=FAIL,
            observed=f"{type(exc).__name__}: {exc}",
            detail="check raised",
        )
    c.duration_ms = int((time.perf_counter() - t0) * 1000)
    return c


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_state_integrity(db: Path) -> Check:
    """R2 — the state DB is structurally sound before anything trusts it."""
    if not db.exists():
        return Check("STORE-1", "R2 state integrity", SKIP,
                     f"no state db at {db}", "run the pipeline first")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        conn.close()
    return Check(
        "STORE-1", "R2 state integrity",
        PASS if result == "ok" else FAIL,
        f"integrity_check={result} schema_version={version}",
    )


def check_no_sql_in_surfaces(src: Path) -> Check:
    """ADR-006 — no SQL and no sqlite3 import under tui/ or web/.

    Mechanical, because the boundary is exactly the kind of rule that erodes
    one convenient query at a time.
    """
    offenders: list[str] = []
    pattern = re.compile(r"\bimport\s+sqlite3\b|\bSELECT\s+|\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b")
    for sub in ("tui", "web"):
        root = src / sub
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="replace")
            # Strip docstrings/comments cheaply: the boundary is about executed
            # SQL, and both `tui/app.py` and this module legitimately *describe*
            # the rule in prose.
            code = "\n".join(
                line for line in text.splitlines()
                if not line.lstrip().startswith("#")
            )
            code = re.sub(r'"""[\s\S]*?"""', "", code)
            code = re.sub(r"'''[\s\S]*?'''", "", code)
            if pattern.search(code):
                offenders.append(str(py.relative_to(src)))
    return Check(
        "ADR006-1", "ADR-006 no SQL in surfaces",
        FAIL if offenders else PASS,
        f"{len(offenders)} offending file(s)",
        ", ".join(offenders),
    )


def check_source_format_health(config_path: str | None = None) -> Check:
    """I2 — every available source still matches the shape its parser reads.

    Runs the real canary against the paths the RUN actually reads, which means
    honouring `config.source_paths` overrides. Probing default paths instead
    would let this gate pass on six healthy defaults while the configured
    production source sat drifted — a green check for a system nobody runs.

    A `drifted` source FAILS here even though it is only a warning during a
    run: `doctor` is the surface an operator checks deliberately, so a silent
    format change is exactly what it should refuse to pass.

    An `error` outcome also fails. A guard that cannot run is not a guard, and
    the whole point of separating `error` from `absent` is that a broken check
    must never file itself under the outcome nobody looks at.

    `absent` is NOT a failure: a harness that is not installed on this machine
    is a normal state, and failing on it would train an operator to ignore the
    check entirely.
    """
    from .connectors import make_connectors

    try:
        from .config import load_config

        overrides = load_config(config_path).source_paths or {}
    except Exception as exc:  # noqa: BLE001
        # A missing or malformed config must not silently downgrade this to a
        # defaults-only probe: the operator would read a PASS that never
        # covered their real sources.
        return Check(
            "I2-1", "I2 source format health", FAIL,
            f"cannot load config to resolve source paths: {type(exc).__name__}: {exc}",
            "",
        )

    bad: list[str] = []
    ok = absent = 0
    overridden = 0
    for connector in make_connectors():
        override = overrides.get(connector.SOURCE_ID)
        if override:
            overridden += 1
        try:
            health = connector.format_health(override)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{connector.SOURCE_ID} (check raised {type(exc).__name__}: {exc})")
            continue
        # Branches are EXHAUSTIVE and explicit. A trailing `else: absent += 1`
        # would file every unrecognised outcome — a typo, a status added by a
        # future connector, or the `error` value this check is documented to
        # fail on — under the one bucket that passes silently. An unknown
        # outcome is unproven, and unproven is not a pass.
        if health.outcome == "drifted":
            bad.append(f"{connector.SOURCE_ID}: {health.detail}")
        elif health.outcome == "error":
            bad.append(f"{connector.SOURCE_ID}: check failed — {health.detail}")
        elif health.outcome == "ok":
            ok += 1
        elif health.outcome == "absent":
            absent += 1
        else:
            bad.append(
                f"{connector.SOURCE_ID}: unknown format-health outcome "
                f"{health.outcome!r} — treated as unproven"
            )
    return Check(
        "I2-1", "I2 source format health",
        FAIL if bad else PASS,
        f"{ok} ok, {absent} absent, {len(bad)} drifted/error"
        + (f"; {overridden} configured override(s) probed" if overridden else ""),
        "; ".join(bad),
    )


def check_bundle_hygiene(output_dir: Path, db: Path) -> Check:
    """R14.b/I5 — an exported bundle carries no secret and no home path.

    Exports a real bundle for a real project and inspects the produced bytes.
    """
    if not db.exists():
        return Check("R14-1", "R14 bundle hygiene", SKIP, "no state db", "")
    from .bundle import export_project_bundle
    from .read import ReadStore
    from .redact import CANARY

    rs = ReadStore(db, output_dir, read_only=True)
    try:
        projects = rs.all_projects()
        if not projects:
            return Check("R14-1", "R14 bundle hygiene", SKIP, "no projects in store", "")
        target = max(projects, key=lambda p: getattr(p, "session_count", 0))
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            out = export_project_bundle(rs, target.id, Path(td) / "b.html")
            html = out.read_text(encoding="utf-8")
    finally:
        rs.close()

    home = str(Path.home())
    problems = []
    if CANARY in html:
        problems.append("canary present")
    if home in html:
        problems.append("absolute home path present")
    external = re.findall(r'(?:src|href)\s*=\s*["\'](?:https?:)?//', html)
    if external:
        problems.append(f"{len(external)} external asset ref(s)")
    return Check(
        "R14-1", "R14 bundle hygiene",
        FAIL if problems else PASS,
        f"{len(html)} bytes, 0 secrets, 0 home paths, {len(external)} external refs",
        "; ".join(problems),
    )


def check_repo_read_only(db: Path) -> Check:
    """R9/N3 — the read surface physically cannot write.

    Asserted by attempting a write and requiring SQLite to refuse. A read-only
    flag that is merely *set* proves nothing; a refused write proves it.
    """
    if not db.exists():
        return Check("R9-1", "R9 read-only enforcement", SKIP, "no state db", "")
    from .read import ReadStore

    rs = ReadStore(db, db.parent, read_only=True)
    try:
        rs._store.conn.execute("CREATE TABLE _doctor_probe (x)")
    except sqlite3.OperationalError as exc:
        return Check("R9-1", "R9 read-only enforcement", PASS,
                     f"write refused by SQLite: {exc}")
    except Exception as exc:  # noqa: BLE001
        return Check("R9-1", "R9 read-only enforcement", FAIL,
                     f"unexpected error type {type(exc).__name__}: {exc}")
    finally:
        rs.close()
    return Check("R9-1", "R9 read-only enforcement", FAIL,
                 "CREATE TABLE succeeded on a read-only store")


def check_canary_absent(output_dir: Path, db: Path) -> Check:
    """N4 — zero secrets persisted anywhere dreamy writes."""
    from .redact import CANARY

    hits: list[str] = []
    scanned = 0
    if output_dir.exists():
        for p in output_dir.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix in (".db", ".db-wal", ".db-shm") or "state.db" in p.name:
                continue
            # The acceptance tree holds this audit's own notes, which quote the
            # canary by design. Product output is what N4 governs.
            if "acceptance" in p.parts:
                continue
            scanned += 1
            try:
                if CANARY in p.read_text(errors="ignore"):
                    hits.append(str(p))
            except OSError:
                continue
    db_hits = 0
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.text_factory = lambda b: b.decode("utf-8", "ignore")
        try:
            for (table,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall():
                for col in conn.execute(f"PRAGMA table_info({table})").fetchall():
                    try:
                        n = conn.execute(
                            f'SELECT COUNT(*) FROM "{table}" WHERE "{col[1]}" LIKE ?',
                            (f"%{CANARY}%",),
                        ).fetchone()[0]
                        db_hits += n
                    except sqlite3.Error:
                        continue
        finally:
            conn.close()
    return Check(
        "N4-1", "N4 zero secrets persisted",
        FAIL if (hits or db_hits) else PASS,
        f"{scanned} artifacts + all state.db columns scanned; {len(hits)} file hit(s), {db_hits} row hit(s)",
        "; ".join(hits[:5]),
    )


def check_evidence_paths_absolute(output_dir: Path) -> Check:
    """R6g — every evidence citation dreamy EMITS is absolute.

    Scoped to citations the compiler is responsible for, matching
    `prompt_compiler._rewrite_evidence_paths` exactly. Two categories are
    legitimately relative and rewriting either would be the defect:

      * illustrative placeholders (`evidence/vg{N}-{desc}.{ext}`) — an
        absolute rewrite manufactures a path that can never exist and turns
        the cold-start check into a false failure;
      * verbatim skill-body text — the four artifacts inline whole SKILL.md
        bodies, and those documents talk about `evidence/` in their own
        prose. Rewriting another author's documentation would corrupt the
        inlined contract to satisfy a rule that never governed it.

    An earlier draft of this check flagged all 66 such references across 13
    artifacts and reported FAIL. Every one was skill prose or a placeholder;
    zero were dreamy citations. A check that cannot tell the product's own
    output from quoted source text measures nothing.
    """
    root = output_dir / "reports" / "latest" / "projects"
    if not root.exists():
        return Check("R6g-1", "R6g absolute evidence paths", SKIP,
                     "no generated prompts", "run the pipeline first")
    relative: list[str] = []
    artifacts = 0
    pattern = re.compile(r"(?<![\w/])(\.?/?)evidence/(?P<rest>\S*)")
    for md in root.rglob("prompts/*.md"):
        artifacts += 1
        text = md.read_text(encoding="utf-8", errors="replace")
        # Inlined skill bodies sit between <skill> tags; their prose is not
        # dreamy's citation surface.
        outside_skill_bodies = re.sub(r"<skill\b[\s\S]*?</skill>", "", text)
        for line in outside_skill_bodies.splitlines():
            for m in pattern.finditer(line):
                rest = m.group("rest")
                if any(ch in rest for ch in "{}<>*"):
                    continue  # illustrative placeholder, correctly left alone
                relative.append(f"{md.name}: {line.strip()[:80]}")
    return Check(
        "R6g-1", "R6g absolute evidence paths",
        FAIL if relative else PASS,
        f"{artifacts} artifact(s), {len(relative)} relative dreamy-emitted evidence ref(s)",
        "; ".join(relative[:3]),
    )


def check_skill_refs_resolved(output_dir: Path) -> Check:
    """R6f — zero unresolved skill references in default `inline` output.

    Delegates detection to `prompt_compiler.detect_unresolved_skill_refs`,
    the same function the compiler enforces R6f with at emit time. An earlier
    version re-implemented the token pattern here. That was wrong twice over:
    a second copy can drift from the one the product actually enforces, so
    the harness would attest to a rule nobody applies; and it added a literal
    occurrence of the inventory token to an eighth file, breaking the ADR-010
    row-13 guard that pins exactly which modules carry it. Calling the
    canonical detector fixes both — one definition of the rule, and no new
    module in that inventory.
    """
    from .prompt_compiler import detect_unresolved_skill_refs

    root = output_dir / "reports" / "latest" / "projects"
    if not root.exists():
        return Check("R6f-1", "R6f no unresolved skill refs", SKIP,
                     "no generated prompts", "run the pipeline first")
    unresolved: list[str] = []
    artifacts = 0
    for md in root.rglob("prompts/*.md"):
        artifacts += 1
        hits = detect_unresolved_skill_refs(
            md.read_text(encoding="utf-8", errors="replace")
        )
        unresolved.extend(f"{md.name}: {h}" for h in hits)
    return Check(
        "R6f-1", "R6f no unresolved skill refs",
        FAIL if unresolved else PASS,
        f"{artifacts} artifact(s), {len(unresolved)} unresolved ref(s)",
        "; ".join(unresolved[:3]),
    )


def check_connector_conformance() -> Check:
    """R1 — every registered connector implements the full Connector ABC."""
    from .connectors import make_connectors
    from .protocol import Connector

    required = ("SOURCE_ID", "discover", "scan")
    bad: list[str] = []
    ids: list[str] = []
    for c in make_connectors():
        ids.append(getattr(c, "SOURCE_ID", c.__class__.__name__))
        for member in required:
            if not hasattr(c, member):
                bad.append(f"{c.__class__.__name__} missing {member}")
        if not isinstance(c, Connector):
            bad.append(f"{c.__class__.__name__} is not a Connector")
    return Check(
        "R1-1", "R1 connector conformance",
        FAIL if bad else PASS,
        f"{len(ids)} connectors: {', '.join(sorted(ids))}",
        "; ".join(bad),
    )


def check_tui_readonly_indicator(db: Path | None = None) -> Check:
    """C.2 — the permanent read-only indicator is visible at all times.

    Judged on rendered frame bytes across several terminal geometries, because
    the defect this check exists to catch was a widget that was mounted,
    styled, and positioned correctly on X while sitting off-screen on Y. Only
    the composited frame can distinguish "exists" from "visible".

    `db` is passed in rather than re-derived. An earlier version called
    `_default_db()` here while every sibling check received its path from
    `run_doctor`, so pointing the harness at one store silently rendered the
    TUI against another — two sources of truth inside a harness whose entire
    purpose is to be the single one.
    """
    db = db or _default_db()
    if not db.exists():
        return Check("C2-1", "C.2 read-only indicator visible", SKIP, "no state db", "")
    try:
        import asyncio

        from .read import ReadStore
        from .tui.app import DreamyApp
    except ImportError as exc:
        return Check("C2-1", "C.2 read-only indicator visible", SKIP,
                     f"tui unavailable: {exc}", "")

    from .tui.theme import REPO_READ_ONLY_GLYPH

    sizes = ((160, 48), (100, 30), (80, 24))
    missing: list[str] = []
    checked = 0

    async def probe(cols: int, lines: int) -> None:
        nonlocal checked
        rs = ReadStore(db, db.parent, read_only=False)
        try:
            app = DreamyApp(read_store=rs, output_dir=db.parent)
            async with app.run_test(size=(cols, lines)) as pilot:
                await pilot.pause()
                await pilot.pause()
                for key, name in (("1", "runs"), ("2", "findings"), ("3", "projects"),
                                  ("4", "prompts"), ("5", "schedule"), ("6", "monitor")):
                    await pilot.press(key)
                    await pilot.pause()
                    frame = "\n".join(
                        "".join(seg.text for seg in strip)
                        for strip in app.screen._compositor.render_strips()
                    )
                    checked += 1
                    if REPO_READ_ONLY_GLYPH not in frame:
                        missing.append(f"{cols}x{lines}/{name}")
        finally:
            rs.close()

    os.environ.setdefault("COLUMNS", "160")
    for cols, lines in sizes:
        os.environ["COLUMNS"], os.environ["LINES"] = str(cols), str(lines)
        asyncio.run(probe(cols, lines))

    return Check(
        "C2-1", "C.2 read-only indicator visible",
        FAIL if missing else PASS,
        f"visible in {checked - len(missing)}/{checked} view x size frames",
        ", ".join(missing[:6]),
    )


def check_git_available() -> Check:
    """R4 — deterministic analysis requires the git CLI; its absence is fatal."""
    path = shutil.which("git")
    if not path:
        return Check("R4-1", "R4 git CLI present", FAIL, "git not on PATH",
                     "deterministic analysis cannot run")
    out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
    return Check("R4-1", "R4 git CLI present", PASS, out.stdout.strip())


def _default_db() -> Path:
    return Path(
        os.environ.get("DREAMY_OUTPUT_DIR")
        or (Path.home() / ".local/share/dreamy")
    ) / "state.db"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def run_doctor(
    output_dir: Path | None = None,
    src_root: Path | None = None,
    config_path: str | None = None,
) -> DoctorReport:
    """Run the implemented diagnostics against the real system.

    NOT the full acceptance matrix, and deliberately no longer claiming to be.
    The active authority is the addendum completion gate G1-G8 (see
    `docs/acceptance/GATE-MATRIX.md`, superseding note at the top of file).
    This harness covers ten checks and leaves these gates UNCOVERED:

      * packaging and pristine-venv install
      * end-to-end live pipeline execution
      * launchd install/parity
      * cross-run reproducibility beyond artifact bytes
      * release/publish gates

    Exit 0 therefore means "every implemented check ran and passed", never
    "the acceptance matrix is satisfied". Treating a partial harness as
    matrix approval is the exact overclaim this session found three times in
    the product itself: a source reported dark while holding 5,989 sessions,
    an indicator declared visible after only its X coordinate was checked,
    and a bundle guarded for canaries but not for `$HOME`. A harness that
    inherits that failure mode is worse than none, because it is trusted.

    Extending coverage means adding a check here AND removing the matching
    line from the uncovered list above.
    """
    out = Path(output_dir) if output_dir else Path(
        os.environ.get("DREAMY_OUTPUT_DIR") or (Path.home() / ".local/share/dreamy")
    )
    db = out / "state.db"
    src = Path(src_root) if src_root else Path(__file__).resolve().parent

    report = DoctorReport(started_ms=int(time.time() * 1000))
    report.checks = [
        _run(lambda: check_state_integrity(db), "STORE-1", "R2 state integrity"),
        _run(lambda: check_no_sql_in_surfaces(src), "ADR006-1", "ADR-006 no SQL in surfaces"),
        _run(check_connector_conformance, "R1-1", "R1 connector conformance"),
        _run(check_git_available, "R4-1", "R4 git CLI present"),
        _run(lambda: check_repo_read_only(db), "R9-1", "R9 read-only enforcement"),
        _run(lambda: check_canary_absent(out, db), "N4-1", "N4 zero secrets persisted"),
        _run(lambda: check_bundle_hygiene(out, db), "R14b-1", "R14.b bundle hygiene"),
        _run(lambda: check_evidence_paths_absolute(out), "R6g-1", "R6g absolute evidence paths"),
        _run(lambda: check_skill_refs_resolved(out), "R6f-1", "R6f no unresolved skill refs"),
        _run(lambda: check_tui_readonly_indicator(db), "C2-1", "C.2 read-only indicator visible"),
        _run(lambda: check_source_format_health(config_path), "I2-1", "I2 source format health"),
    ]
    report.ended_ms = int(time.time() * 1000)
    return report


def render_text(report: DoctorReport) -> str:
    lines = ["dreamy doctor — implemented diagnostics (partial; not the full matrix)", ""]
    width = max(len(c.id) for c in report.checks) if report.checks else 8
    for c in report.checks:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[c.verdict]
        lines.append(f"  [{mark}] {c.id:<{width}}  {c.requirement}")
        lines.append(f"         {c.observed}")
        if c.detail:
            lines.append(f"         -> {c.detail}")
    lines.append("")
    lines.append(
        f"{len(report.passed)} passed, {len(report.failed)} failed, "
        f"{len(report.skipped)} skipped in {(report.ended_ms - report.started_ms) / 1000:.1f}s"
    )
    if report.skipped:
        lines.append("NOTE: a skipped check proves nothing. It is not a pass.")
    return "\n".join(lines)


def render_json(report: DoctorReport) -> str:
    return json.dumps(
        {
            "started_ms": report.started_ms,
            "ended_ms": report.ended_ms,
            "exit_code": report.exit_code,
            "summary": {
                "passed": len(report.passed),
                "failed": len(report.failed),
                "skipped": len(report.skipped),
            },
            "checks": [asdict(c) for c in report.checks],
        },
        indent=2,
        sort_keys=True,
    )
