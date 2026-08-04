from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from .config import load_config, resolve_path


def _source_paths(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        source, separator, path = value.partition("=")
        if not separator or not source or not path:
            raise ValueError(f"invalid --source-path {value!r}; expected SOURCE=PATH")
        result[source] = path
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dreamy", description="Read-only cross-harness coding-session reconciler")
    parser.add_argument("--config", help="config.json path")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="reconcile real harness stores")
    run.add_argument("--lookback-days", type=int)
    run.add_argument("--source-path", action="append", default=[], metavar="SOURCE=PATH")
    run.add_argument("--agent-cap-usd", type=float, help="temporary positive per-run agent cap")

    sub.add_parser("install", help="install or refresh com.nick.dreamy launchd job")
    sub.add_parser("uninstall", help="uninstall com.nick.dreamy launchd job")
    sub.add_parser("status", help="emit machine-readable state and schedule status")
    skills = sub.add_parser("skills", help="inspect, refresh, or verify pinned skill bodies")
    skills.add_argument("skills_action", choices=("status", "refresh", "verify"))
    skills.add_argument(
        "--lock",
        default=None,
        help="skill lock manifest for 'verify' (default: the packaged lock)",
    )
    sub.add_parser("tui", help="open Textual dashboard")

    web = sub.add_parser("web", help="serve the read-only loopback dashboard (R17)")
    web.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; must resolve to loopback only (default: 127.0.0.1)",
    )
    web.add_argument("--port", type=int, default=8765, help="bind port (default: 8765)")

    # R-0: before this, `validate_run_manifest` had ZERO production consumers.
    # Its only callers were tests, and the one gate test skips unless an env var
    # is set -- so a fully green suite was compatible with the validator never
    # running. A contract nothing invokes gates nothing.
    verify = sub.add_parser(
        "verify-run",
        help="validate an acceptance run manifest; exit 0 admits, 2 rejects",
    )
    verify.add_argument("manifest", help="path to the run manifest JSON")
    verify.add_argument(
        "--repo-root",
        default=".",
        help="repository root holding GATE-MATRIX.md and the specification",
    )

    cost = sub.add_parser("cost", help="R10 cost rollup; --project scopes to a single project")
    cost.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    cost.add_argument("--project", help="project id; emits that project's 30-day cost")
    cost.add_argument(
        "--window",
        choices=("daily", "monthly"),
        default=None,
        help="I3: cumulative spend_ledger rollup (trailing 24h or 30d) vs configured caps",
    )

    bundle = sub.add_parser("bundle", help="R14 export a self-contained project HTML bundle")
    bundle.add_argument("project_id", help="project id to bundle")
    bundle.add_argument(
        "--output",
        help="output path (default: <output_dir>/bundles/<project_id>.html)",
    )

    efficacy = sub.add_parser(
        "efficacy",
        help="R21 prompt efficacy report; 'not_observed' is never a failure",
    )
    efficacy.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    # Local import: argparse evaluates `choices` eagerly, so the read layer
    # has to be imported here before `dismiss` is configured.
    from .read import ReadStore

    findings = sub.add_parser(
        "findings", help="R13 findings; default state filter is new+regressed"
    )
    findings.add_argument(
        "--state",
        action="append",
        default=None,
        metavar="STATE",
        help=(
            "filter by delta state (new, persisting, resolved, regressed, dismissed). "
            "Repeatable. Pass 'all' (single token) to disable filtering. Default: new,regressed."
        ),
    )
    findings.add_argument("--severity", help="filter by severity")
    findings.add_argument("--category", help="filter by category")
    findings.add_argument("--project", help="filter by project id")
    findings.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    findings.add_argument("--limit", type=int, default=None, help="cap rows returned")
    findings.add_argument(
        "--metrics",
        action="store_true",
        help=(
            "R22 lifecycle metrics per category and severity (time-to-resolve, "
            "regression rate, backlog delta, age p50/p90); ignores --state/--limit"
        ),
    )

    dismiss = sub.add_parser("dismiss", help="R20 dismiss a finding with a reason code")
    dismiss.add_argument("finding_id", help="finding id to dismiss")
    dismiss.add_argument(
        "--reason",
        required=True,
        choices=ReadStore.DISMISSAL_REASONS,
        help="one of the four R20 dismissal codes (mirrors ReadStore.DISMISSAL_REASONS)",
    )

    undismiss = sub.add_parser("undismiss", help="R20 reverse a previous dismissal")
    undismiss.add_argument("finding_id", help="finding id to undismiss")


    doctor = sub.add_parser(
        "doctor",
        help=(
            "I1 run the implemented acceptance diagnostics (PARTIAL — not the "
            "full matrix; see docs/acceptance/GATE-MATRIX.md); exit 0 = all "
            "implemented checks ran and passed, 1 = failed, 2 = incomplete"
        ),
    )
    doctor.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    imp = sub.add_parser(
        "import",
        help=(
            "I5 import a bundle HTML file as an archival snapshot (NOT a project "
            "restore); exit 0 = imported or already present, 2 = malformed/tampered"
        ),
    )
    imp.add_argument("path", help="bundle .html produced by `dreamy bundle`")
    imp.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    archives = sub.add_parser(
        "archives", help="I5 list imported bundle snapshots"
    )
    archives.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    diff_p = sub.add_parser("diff", help="R13 diff between the latest two runs")
    diff_p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def _chain_skill_names(cfg) -> list[str]:
    """Every skill the compiler may inline, chains plus shared constraints."""
    from .prompt_compiler import DEFAULT_CHAINS

    chains = dict(DEFAULT_CHAINS)
    chains.update(getattr(cfg, "prompt_chains", None) or {})
    names: list[str] = []
    for value in chains.values():
        for name in value or []:
            if name not in names:
                names.append(name)
    return names


def _skills_verify(lock_path: str | None) -> int:
    """Check the lock against the filesystem. Exit 5 when a required skill fails.

    Distinct from `status`, which compares against pins in the run database.
    This answers the question the database cannot on a machine that has never
    run dreamy: is every required skill present, at its pinned hash?
    """
    from .skills_lock import LockError, default_lock_path, verify_lock

    try:
        target = Path(lock_path) if lock_path else default_lock_path()
        report = verify_lock(target)
    except LockError as exc:
        print(f"dreamy: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    if not report.ok:
        for failure in report.failures:
            print(
                f"dreamy: required skill '{failure.name}' {failure.status}: "
                f"{failure.remediation}",
                file=sys.stderr,
            )
        return 5
    return 0


def _skills(cfg, output_dir: Path, action: str) -> int:
    """Report or update the content-addressed skill pins.

    Q11 is resolved as PIN: artifacts inline a pinned snapshot so two unchanged
    runs stay byte-identical. Without an explicit refresh the pin is a one-way
    trap — a skill improvement could never reach future prompts.
    """
    from . import skill_pins
    from .store import Store

    store = Store(output_dir / "state.db")
    try:
        report: list[dict[str, str | bool | None]] = []
        for name in _chain_skill_names(cfg):
            body = skill_pins.read_skill_body(name)
            if body is None:
                report.append({"skill": name, "status": "missing_on_disk"})
                continue
            current = skill_pins._sha256_hex(body)
            pinned = skill_pins.read_skill_pin_record(store, name)
            drifted = skill_pins.drift_check(store, name, current)
            entry = {
                "skill": name,
                "current_sha256": current[:16],
                "pinned_sha256": (pinned or {}).get("content_sha256", "")[:16] or None,
                "drifted": drifted,
            }
            if action == "refresh" and drifted:
                skill_pins.pin_skill(store, name)
                entry["status"] = "repinned"
            else:
                entry["status"] = "drifted" if drifted else "current"
            report.append(entry)
        store.commit()
        print(json.dumps({"action": action, "skills": report}, indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


def _verify_run(manifest: str, repo_root: str) -> int:
    """Validate a run manifest from the command line.

    Exit 2 on rejection rather than 1: a rejected manifest is a refused claim,
    not a crash, and callers gate on it.
    """
    # The acceptance-runs base is deliberately NOT a parameter. Exposing it
    # would let the party making the claim relocate the trust boundary the
    # containment check exists to enforce; tests inject it directly instead.
    from .acceptance import ManifestError, validate_run_manifest

    try:
        run = validate_run_manifest(Path(manifest), Path(repo_root))
    except ManifestError as exc:
        print(f"dreamy: run manifest REJECTED: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "verdict": "ADMITTED",
                "run_id": run.run_id,
                "gate": run.gate,
                "requirement_ids": list(run.requirement_ids),
                "verified_hashes": run.verified_hashes,
                "reviewer": run.reviewer,
            },
            indent=2,
        )
    )
    return 0


def _cost(
    output_dir: Path, as_json: bool, project_id: str | None, window: str | None = None,
    cfg=None,
) -> int:
    """R10 cost rollup. With --project, emit one project's 30-day cost.

    The unattributed bucket is shown alongside the attributed total: most
    router requests carry no defensible session link, so hiding it would
    silently understate total spend.

    I3: with --window, emit a cumulative spend_ledger rollup instead —
    trailing 24h for daily, trailing 30d for monthly — against the
    configured cap so an operator can see exactly what
    `run_pipeline`'s pre-flight check itself compares.
    """
    from .read import ReadStore

    state = ReadStore(output_dir / "state.db", output_dir=output_dir)
    try:
        if window is not None:
            now_ms = int(time.time() * 1000)
            if window == "daily":
                since_ms = now_ms - 24 * 3600 * 1000
                cap = getattr(cfg, "spend_cap_daily_usd", None) if cfg is not None else None
                label = "24h"
            else:
                since_ms = now_ms - 30 * 24 * 3600 * 1000
                cap = getattr(cfg, "spend_cap_monthly_usd", None) if cfg is not None else None
                label = "30d"
            spent = state._store.spend_since(since_ms)
            remaining = max(cap - spent, 0.0) if cap is not None else None
            payload = {
                "window": window,
                "spent_usd": spent,
                "cap_usd": cap,
                "remaining_usd": remaining,
            }
            if as_json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"Cumulative cost — {window} (trailing {label})")
                print(f"  spent      ${spent:>10.4f}")
                if cap is not None:
                    print(f"  cap        ${cap:>10.4f}")
                    print(f"  remaining  ${remaining:>10.4f}")
                else:
                    print(f"  cap        (not configured — spend_cap_{window}_usd is null)")
            return 0

        if project_id is not None:
            if state.project_detail(project_id) is None:
                print(f"dreamy: unknown project id: {project_id}", file=sys.stderr)
                return 2
            detail = state.cost_30d(project_id)
            if detail is None:
                # Unreachable in practice: `project_detail` above already
                # rejected unknown ids, and `cost_30d`'s aggregate is
                # COALESCE-guarded so it always yields a row. Handled anyway
                # because the alternative is an AttributeError traceback on a
                # user-facing command, and a project with genuinely no router
                # requests is a legitimate state, not an error.
                #
                # Zeroed rather than skipped: R10 requires the rollup to state
                # a project's spend, and omitting the project would read as
                # "not measured" rather than "measured, nothing spent".
                print(
                    json.dumps({
                        "project_id": project_id, "total_usd": 0.0,
                        "episode_count": 0, "per_completed_intent_usd": 0.0,
                        "confidence_mix": {},
                    }, indent=2, sort_keys=True)
                    if as_json
                    else f"30-day cost — project {project_id}\n  no router requests attributed"
                )
                return 0
            payload = {
                "project_id": detail.project_id,
                "total_usd": detail.total_usd,
                "episode_count": detail.episode_count,
                "per_completed_intent_usd": detail.per_completed_intent_usd,
                "confidence_mix": dict(sorted(detail.confidence_mix.items())),
            }
            if as_json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"30-day cost — project {detail.project_id}")
                print(f"  total                ${detail.total_usd:>10.4f}")
                print(f"  episode_count        {detail.episode_count:>11d}")
                print(f"  per_completed_intent ${detail.per_completed_intent_usd:>10.4f}")
                print(f"  confidence_mix       {dict(sorted(detail.confidence_mix.items()))}")
            return 0

        rollup = state.cost_rollup()
        if as_json:
            print(json.dumps(rollup, indent=2, sort_keys=True))
        else:
            print("Cost rollup")
            print(f"  attributed    ${rollup['attributed_usd']:>10.4f}")
            print(
                f"  unattributed  ${rollup['unattributed_usd']:>10.4f}  "
                f"({rollup['unattributed_requests']} requests with no defensible session link)"
            )
            print(f"  confidence    {dict(sorted(rollup['confidence_mix'].items()))}")
            print()
            print(f"  {'project':<32}  {'total_usd':>12}  {'requests':>10}")
            for row in rollup["projects"]:
                print(f"  {row['name'][:32]:<32}  ${row['total_usd']:>11.4f}  {row['requests']:>10d}")
        return 0
    finally:
        state.close()


def _bundle(output_dir: Path, project_id: str, output: str | None) -> int:
    """R14 self-contained project bundle. One HTML file, no external assets."""
    from .bundle import export_project_bundle
    from .read import ReadStore

    target = (Path(output).resolve() if output else (output_dir / "bundles" / f"{project_id}.html")).resolve()
    state = ReadStore(output_dir / "state.db", output_dir=output_dir)
    try:
        try:
            written = export_project_bundle(state, project_id, target)
        except ValueError as exc:
            print(f"dreamy: {exc}", file=sys.stderr)
            return 2
    finally:
        state.close()
    print(str(written))
    return 0


def _efficacy(output_dir: Path, as_json: bool) -> int:
    """R21 prompt efficacy. Needs the raw Store, not ReadStore."""
    from .efficacy import report
    from .store import Store

    store = Store(output_dir / "state.db")
    try:
        payload = report(store)
    finally:
        store.close()
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("Efficacy (R21)")
        print()
        print(f"  {'artifact_type':<14}  {'total':>6}  {'observed':>8}  {'not_observed':>12}  {'rate':>7}")
        for art_type, entry in payload["artifact_types"].items():
            rate = entry["observation_rate"]
            rate_str = "" if rate is None else f"{rate:.4f}"
            print(
                f"  {art_type:<14}  {entry['total']:>6d}  {entry['observed']:>8d}  "
                f"{entry['not_observed']:>12d}  {rate_str:>7}"
            )
            print(f"    harnesses: {entry['harnesses']}")
            print(
                f"    completed_after_use: {entry['completed_after_use']}  "
                f"unresolved_after_use: {entry['unresolved_after_use']}"
            )
        print()
        print(f"note: {payload['note']}")
    return 0


def _findings(
    output_dir: Path,
    states: list[str] | None,
    severity: str | None,
    category: str | None,
    project: str | None,
    as_json: bool,
    limit: int | None,
    metrics: bool = False,
) -> int:
    """R13 findings. Default delta-state filter is new+regressed.

    `--metrics` (R22) renders lifecycle stats instead of the row list and
    ignores --state/--limit/--project — the metrics are already grouped by
    category/severity, so a state filter would just delete rows before the
    aggregation, and --project has no bucket to attach to.
    """
    from .read import ReadStore

    if metrics:
        state = ReadStore(output_dir / "state.db", output_dir=output_dir)
        try:
            grouped = state.finding_metrics()
        finally:
            state.close()
        if as_json:
            print(json.dumps(
                {
                    dim: [
                        {
                            "key": m.key,
                            "not_observed": m.not_observed,
                            "resolve_events": m.resolve_events,
                            "time_to_resolve_ms": m.time_to_resolve_ms,
                            "regression_rate": m.regression_rate,
                            "backlog_delta": m.backlog_delta,
                            "open_count": m.open_count,
                            "age_p50_ms": m.age_p50_ms,
                            "age_p90_ms": m.age_p90_ms,
                        }
                        for m in rows
                    ]
                    for dim, rows in grouped.items()
                },
                indent=2,
                sort_keys=True,
            ))
        else:
            for dim, label in (("by_category", "Category"), ("by_severity", "Severity")):
                print(f"Finding metrics — {label} (R22)")
                print(
                    f"  {label.lower():<22}  {'ttr_ms':>12}  {'regr_rate':>9}  "
                    f"{'backlog_Δ':>9}  {'open':>5}  {'age_p50_ms':>12}  "
                    f"{'age_p90_ms':>12}  {'not_obs':>7}"
                )
                for m in grouped[dim]:
                    ttr = "" if m.time_to_resolve_ms is None else f"{m.time_to_resolve_ms:.0f}"
                    rr = "" if m.regression_rate is None else f"{m.regression_rate:.4f}"
                    p50 = "" if m.age_p50_ms is None else f"{m.age_p50_ms:.0f}"
                    p90 = "" if m.age_p90_ms is None else f"{m.age_p90_ms:.0f}"
                    print(
                        f"  {m.key:<22}  {ttr:>12}  {rr:>9}  {m.backlog_delta:>9}  "
                        f"{m.open_count:>5}  {p50:>12}  {p90:>12}  {m.not_observed:>7}"
                    )
                print()
        return 0

    if limit is not None and limit <= 0:
        print("dreamy: --limit must be positive", file=sys.stderr)
        return 2

    flt: dict = {}
    if severity:
        flt["severity"] = severity
    if category:
        flt["category"] = category
    if project:
        flt["project_id"] = project

    # `--state all` (single token) disables the state filter; otherwise the
    # supplied list is used verbatim. With no --state at all, default to
    # new+regressed per the R13 spec.
    valid_states = {"new", "persisting", "resolved", "regressed", "dismissed"}
    if states is not None:
        # `all` is a single-token override; mixing it with other values is
        # a caller error rather than a silent `all`-wins.
        if states == ["all"]:
            pass
        elif "all" in states:
            print(
                "dreamy: --state all must be the only token; pass one of "
                "new,persisting,resolved,regressed,dismissed instead",
                file=sys.stderr,
            )
            return 2
        else:
            invalid = [s for s in states if s not in valid_states]
            if invalid:
                print(
                    f"dreamy: unknown --state value(s): {', '.join(invalid)} "
                    f"(expected one of: {', '.join(sorted(valid_states))}, or 'all')",
                    file=sys.stderr,
                )
                return 2
            flt["state"] = list(states)
    else:
        flt["state"] = ["new", "regressed"]

    state = ReadStore(output_dir / "state.db", output_dir=output_dir)
    try:
        rows = state.findings(filter=flt)
    finally:
        state.close()
    if limit is not None:
        rows = rows[:limit]

    if as_json:
        print(json.dumps([
            {
                "id": r.id,
                "project_id": r.project_id,
                "category": r.category,
                "severity": r.severity,
                "title": r.title,
                "delta_state": r.delta_state,
                "provenance": f"({r.provenance})",
                "dismissal_reason": r.dismissal_reason,
            }
            for r in rows
        ], indent=2, sort_keys=True))
    else:
        print(f"Findings ({len(rows)} rows)")
        print(f"  {'id':<16}  {'state':<10}  {'prov':<6}  {'sev':<8}  {'category':<14}  title")
        for r in rows:
            prov_tag = f"({r.provenance})"
            print(
                f"  {r.id:<16}  {r.delta_state:<10}  {prov_tag:<6}  "
                f"{r.severity:<8}  {r.category:<14}  {r.title}"
            )
    return 0


def _dismiss(output_dir: Path, finding_id: str, reason: str) -> int:
    """R20 dismiss. Returns 2 when the finding id is unknown."""
    from .read import ReadStore

    state = ReadStore(output_dir / "state.db", output_dir=output_dir)
    try:
        ok = state.dismiss(finding_id, reason)
    finally:
        state.close()
    if not ok:
        print(f"dreamy: unknown finding id: {finding_id}", file=sys.stderr)
        return 2
    print(json.dumps({"finding_id": finding_id, "dismissed": True, "reason": reason}))
    return 0


def _undismiss(output_dir: Path, finding_id: str) -> int:
    """R20 undismiss. Returns 2 when the finding id is unknown."""
    from .read import ReadStore

    state = ReadStore(output_dir / "state.db", output_dir=output_dir)
    try:
        ok = state.undismiss(finding_id)
    finally:
        state.close()
    if not ok:
        print(f"dreamy: unknown finding id: {finding_id}", file=sys.stderr)
        return 2
    print(json.dumps({"finding_id": finding_id, "undismissed": True}))
    return 0


def _diff(output_dir: Path, as_json: bool) -> int:
    """R13 diff between the latest two runs. Pass None for prev to auto-resolve."""
    from .read import ReadStore

    state = ReadStore(output_dir / "state.db", output_dir=output_dir)
    try:
        latest = state.latest_run()
        if latest is None:
            print("dreamy: no runs in state DB", file=sys.stderr)
            return 2
        diff_rows = state.findings_diff(latest.id)
    finally:
        state.close()

    counts: dict[str, int] = {}
    for entry in diff_rows:
        counts[entry.state] = counts.get(entry.state, 0) + 1

    if as_json:
        print(json.dumps({
            "from_run": None,
            "to_run": latest.id,
            "counts": counts,
            "findings": [
                {
                    "finding_id": e.finding_id,
                    "state": e.state,
                    "category": e.category,
                    "severity": e.severity,
                }
                for e in diff_rows
            ],
        }, indent=2, sort_keys=True))
    else:
        print(f"Diff — to run {latest.id}")
        if counts:
            print(f"  counts: {dict(sorted(counts.items()))}")
        else:
            print("  (no findings)")
        for entry in diff_rows:
            print(
                f"  {entry.state:<10}  {entry.severity:<8}  {entry.category:<14}  {entry.finding_id}"
            )
    return 0


def _import(output_dir: Path, path: str, as_json: bool) -> int:
    """I5 — import a bundle as an ARCHIVAL SNAPSHOT.

    Exit 0 on success or on an identical re-import (idempotent). Exit 2 on a
    malformed, oversized, or tampered bundle — a rejection is a real outcome
    an operator must be able to detect from a script, not a warning to skim.
    """
    from .importer import BundleFormatError, import_bundle
    from .store import Store

    store = Store(output_dir / "state.db")
    try:
        result = import_bundle(store, Path(path))
    except BundleFormatError as exc:
        # Basename only, control characters stripped. `path` is operator-typed
        # but the FILENAME can carry ANSI escapes from an untrusted sender, and
        # echoing it raw would let a hostile bundle rewrite the terminal that
        # is reporting its own rejection.
        safe = "".join(ch for ch in Path(path).name if ch.isprintable())[:120]
        print(f"dreamy: refusing to import {safe!r}: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    if as_json:
        print(json.dumps(result.__dict__, sort_keys=True))
    else:
        verb = "already imported" if result.already_imported else "imported"
        print(
            f"{verb}: bundle {result.bundle_id[:16]} "
            f"({result.project_name or 'unnamed'}) — "
            f"{result.sessions_written} session(s), {result.turns_written} turn(s)"
        )
        print("archival snapshot only: timelines and volumes, not a project restore")
    return 0


def _archives(output_dir: Path, as_json: bool) -> int:
    """I5 — list imported snapshots. Read-only."""
    from .read import ReadStore

    # A fresh machine has no state.db, and the read surface is read-only by
    # design (R17d) so it cannot create one. "No store yet" means "no
    # archives", which is a normal state — crashing with a FileNotFoundError
    # traceback would report a missing feature as a broken program.
    db = output_dir / "state.db"
    if not db.exists():
        print(json.dumps([]) if as_json else "no imported bundles")
        return 0

    rs = ReadStore(db, output_dir, read_only=True)
    try:
        rows = rs.imported_bundles()
    finally:
        rs.close()
    if as_json:
        print(json.dumps(rows, sort_keys=True))
        return 0
    if not rows:
        print("no imported bundles")
        return 0
    print(f"Imported snapshots ({len(rows)})")
    print(f"  {'bundle':18s} {'project':24s} {'sessions':>9s} {'turns':>9s}  source")
    for r in rows:
        print(
            f"  {r['bundle_id'][:16]:18s} {(r['project_name'] or '-')[:24]:24s} "
            f"{r['session_count']:>9,} {r['turn_count']:>9,}  {r['source_file']}"
        )
    return 0


def _doctor(output_dir: Path, as_json: bool, config_path: str | None = None) -> int:
    """I1 — run the implemented acceptance diagnostics against the real system.

    PARTIAL by design: this is not the acceptance matrix, whose active
    authority is the addendum completion gate G1-G8. `doctor.run_doctor`
    lists the gates it does not cover. Exit 0 means every implemented check
    ran and passed — never that the matrix is satisfied.

    Exit 0 only when nothing FAILs. A SKIP is reported loudly and never
    counted as a pass: a check that did not run has proven nothing, and
    collapsing the two is how an acceptance harness reports green while
    covering nothing.
    """
    from .doctor import render_json, render_text, run_doctor

    report = run_doctor(output_dir, config_path=config_path)
    print(render_json(report) if as_json else render_text(report))
    return report.exit_code



def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify-run":
        return _verify_run(args.manifest, args.repo_root)
    if not shutil.which("git"):
        print("dreamy: git binary not found on PATH", file=sys.stderr)
        return 127
    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        print(f"dreamy: {exc}", file=sys.stderr)
        return 2
    output_dir = resolve_path(cfg.output_dir)

    if args.command == "run":
        if args.lookback_days is not None and args.lookback_days <= 0:
            print("dreamy: --lookback-days must be positive", file=sys.stderr)
            return 2
        if args.agent_cap_usd is not None:
            if args.agent_cap_usd <= 0:
                print("dreamy: --agent-cap-usd must be positive", file=sys.stderr)
                return 2
            cfg.spend_cap_usd = args.agent_cap_usd
        try:
            overrides = _source_paths(args.source_path)
        except ValueError as exc:
            print(f"dreamy: {exc}", file=sys.stderr)
            return 2
        from .run import run_pipeline

        result = run_pipeline(
            cfg,
            output_dir,
            lookback_days=args.lookback_days,
            source_path_overrides=overrides,
        )
        print(json.dumps(result.__dict__, sort_keys=True))
        return 0 if result.status == "ok" else 3

    from . import launchd

    if args.command == "install":
        # A refusal here is an operator-actionable configuration problem with
        # remediation steps in the message, not a crash. A traceback would bury
        # the instructions under frames the operator cannot act on.
        try:
            plist, message = launchd.install(cfg.interval_seconds)
        except launchd.UndurableInterpreterError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({"plist": str(plist), "launchctl": message, "installed": launchd.is_installed()}))
        return 0 if launchd.is_installed() else 4
    if args.command == "uninstall":
        _, message = launchd.uninstall()
        print(json.dumps({"launchctl": message, "installed": launchd.is_installed()}))
        return 0 if not launchd.is_installed() else 4
    if args.command == "status":
        from .read import ReadStore

        state = ReadStore(output_dir / "state.db", output_dir=output_dir)
        try:
            latest = state.latest_run()
            schedule = state.schedule_state()
            print(json.dumps({
                "latest_run": latest.__dict__ if latest else None,
                "schedule": schedule.__dict__ if schedule else None,
            }, sort_keys=True))
        finally:
            state.close()
        return 0
    if args.command == "skills":
        # `verify` reads the lock and the filesystem only — no run database,
        # so it works on a machine that has never run dreamy. That is exactly
        # the bootstrap case BOOT-002 covers.
        if args.skills_action == "verify":
            return _skills_verify(args.lock)
        return _skills(cfg, output_dir, args.skills_action)
    if args.command == "tui":
        from .tui import launch

        launch(output_dir / "state.db", output_dir)
        return 0
    if args.command == "web":
        # R17a: a non-loopback host is rejected by `build_server` before the
        # socket is opened, so an operator cannot accidentally publish their
        # session history to the network by mistyping a flag.
        from .web import build_server

        try:
            server = build_server(output_dir, args.host, args.port)
        except ValueError as exc:
            print(f"dreamy: {exc}", file=sys.stderr)
            return 2
        # `server_address[0]` is typed as `str | bytes` (the socket family
        # decides). Formatting bytes directly would print `b'127.0.0.1'` into
        # the URL the operator is meant to click.
        raw_host, port = server.server_address[0], server.server_address[1]
        host = raw_host.decode() if isinstance(raw_host, bytes) else raw_host
        print(f"dreamy web: http://{host}:{port}  (read-only; ctrl-c to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\ndreamy web: stopped")
        finally:
            server.server_close()
        return 0
    if args.command == "cost":
        return _cost(output_dir, args.json, args.project, args.window, cfg)
    if args.command == "bundle":
        return _bundle(output_dir, args.project_id, args.output)
    if args.command == "efficacy":
        return _efficacy(output_dir, args.json)
    if args.command == "findings":
        return _findings(
            output_dir,
            args.state,
            args.severity,
            args.category,
            args.project,
            args.json,
            args.limit,
            args.metrics,
        )
    if args.command == "dismiss":
        return _dismiss(output_dir, args.finding_id, args.reason)
    if args.command == "undismiss":
        return _undismiss(output_dir, args.finding_id)
    if args.command == "diff":
        return _diff(output_dir, args.json)
    if args.command == "doctor":
        return _doctor(output_dir, args.json, config_path=args.config)
    if args.command == "import":
        return _import(output_dir, args.path, args.json)
    if args.command == "archives":
        return _archives(output_dir, args.json)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
