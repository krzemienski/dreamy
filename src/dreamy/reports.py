"""Executive report — MD + JSON + self-contained HTML."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Any, Protocol

from .read import ReadStore


class RunResult(Protocol):
    run_id: str
    started_ms: int
    ended_ms: int
    status: str
    source_counts: dict[str, int]
    finding_count: int
    prompt_count: int
    agent_cost_usd: float
    error_count: int
    warning_count: int


_CSS = """
body { background:#0A0A0F; color:#C8C8D4; font-family: ui-monospace, SFMono-Regular, monospace; margin: 2rem; }
h1 { color:#00F0FF; }
h2 { color:#00F0FF; border-bottom:1px solid #1F1F2E; padding-bottom: 0.2rem; }
.card { background:#12121A; border:1px solid #1F1F2E; padding:1rem; border-radius:6px; margin:1rem 0; }
.muted { color:#6A6A80; }
.badge { display:inline-block; padding:2px 6px; border-radius:3px; font-size:0.85em; }
.complete { background:#39FF14; color:#0A0A0F; }
.unverified { background:#FFB000; color:#0A0A0F; }
.abandoned { background:#FF3B47; color:#0A0A0F; }
.in-progress { background:#00F0FF; color:#0A0A0F; }
.drift { background:#FF2E97; color:#0A0A0F; }
table { border-collapse: collapse; width:100%; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #1F1F2E; }
th { color:#A855F7; }
.numeric { text-align: right; font-variant-numeric: tabular-nums; }
"""

_JS = """
function copyPrompt(btn) {
  const block = btn.nextElementSibling;
  const text = block.innerText;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = 'copied';
    setTimeout(() => { btn.textContent = 'copy'; }, 1500);
  });
}
"""


def _html_escape(s: str) -> str:
    return html.escape(str(s), quote=True)


def _summary_row(rs: ReadStore, run_id: str | None = None) -> dict[str, Any]:
    # The report describes THIS run, so read this run's row by id. Falling
    # back to `latest_run()` only when the row is unreadable keeps a report
    # renderable, but the id lookup is what makes the stated window correct
    # under concurrent runs or same-millisecond start ties.
    latest = (rs.run(run_id) if run_id else None) or rs.latest_run()
    projects = rs.all_projects()
    all_f = rs.findings()
    return {
        "latest_run": latest,
        "projects": projects,
        "finding_count": len(all_f),
        "new_count": sum(1 for f in all_f if f.delta_state == "new"),
        "regressed_count": sum(1 for f in all_f if f.delta_state == "regressed"),
    }


def _window_label(summary: dict[str, Any]) -> str:
    lr = summary.get("latest_run")
    days = getattr(lr, "lookback_days", None) if lr else None
    return f"{days}d" if days else "unknown"


def _markdown_report(summary: dict[str, Any], result: RunResult) -> str:
    lines = []
    lines.append(f"# Executive report — {result.run_id}")
    lines.append("")
    lines.append(f"- Status: {result.status}")
    lines.append(f"- Lookback window: {_window_label(summary)}")
    lines.append(f"- Findings: {result.finding_count}")
    lines.append(f"- Prompts: {result.prompt_count}")
    lines.append(f"- Agent cost: ${result.agent_cost_usd:.4f}")
    lines.append(f"- Source counts: {result.source_counts}")
    lines.append("")
    if summary["latest_run"]:
        lr = summary["latest_run"]
        lines.append(f"Latest run: {lr.id} ({lr.status})")
    lines.append("")
    lines.append("## Projects")
    for p in summary["projects"]:
        lines.append(f"- {p.name} — sessions={p.session_count}")
    return "\n".join(lines)


def _html_report(summary: dict[str, Any], result: RunResult) -> str:
    parts = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'><title>Executive report</title>",
        f"<style>{_CSS}</style></head>",
        "<body>",
        f"<h1>Executive report — {_html_escape(result.run_id)}</h1>",
        "<div class='card'>",
        f"<div>Status: <span class='badge complete'>{_html_escape(result.status)}</span></div>",
        f"<div>Lookback window: <span class='numeric'>{_html_escape(_window_label(summary))}</span></div>",
        f"<div>Findings: <span class='numeric'>{result.finding_count}</span> "
        f"Prompts: <span class='numeric'>{result.prompt_count}</span></div>",
        f"<div>Agent cost: <span class='numeric'>${result.agent_cost_usd:.4f}</span></div>",
        f"<div class='muted'>Sources: {', '.join(f'{k}={v}' for k,v in result.source_counts.items())}</div>",
        "</div>",
        "<h2>Projects</h2>",
        "<table><tr><th>Name</th><th>Sessions</th><th>Path</th></tr>",
    ]
    for p in summary["projects"]:
        parts.append(
            f"<tr><td>{_html_escape(p.name)}</td><td class='numeric'>{p.session_count}</td>"
            f"<td class='muted'>{_html_escape(p.path)}</td></tr>"
        )
    parts.append("</table></body></html>")
    return "\n".join(parts)


def write_executive_report(
    output_dir: Path,
    run_id: str,
    cfg,
    result: RunResult,
    read_store: ReadStore,
) -> Path:
    output_dir = Path(output_dir)
    report_dir = output_dir / "reports" / "latest"
    report_dir.mkdir(parents=True, exist_ok=True)
    summary = _summary_row(read_store, run_id)
    window = _window_label(summary)

    rendered = {
        "report.md": _markdown_report(summary, result).encode("utf-8"),
        "report.json": json.dumps({
            "run_id": run_id,
            "status": result.status,
            "lookback_days": getattr(summary["latest_run"], "lookback_days", None)
            if summary["latest_run"] else None,
            "finding_count": result.finding_count,
            "prompt_count": result.prompt_count,
            "agent_cost_usd": result.agent_cost_usd,
            "source_counts": result.source_counts,
            "projects": [
                {"id": p.id, "name": p.name, "sessions": p.session_count, "path": p.path}
                for p in summary["projects"]
            ],
        }, indent=2).encode("utf-8"),
        "report.html": _html_report(summary, result).encode("utf-8"),
    }

    def _atomic(target: Path, payload: bytes) -> None:
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_bytes(payload)
        os.replace(temp, target)

    # R11.c — "Reports from different windows do not overwrite each other in
    # `latest/`." The window-stamped names are the authoritative per-window
    # copies and are the reason the criterion holds: a 90-day run writes
    # `report-90d.*` and leaves an earlier `report-30d.*` byte-intact.
    #
    # The unsuffixed `report.*` names are retained as an alias for the most
    # recent run only, because `latest/` is the well-known path resolved by
    # bundle.py, coldstart.py, evidence_projection.py, prompt_compiler.py and
    # run.py. Removing them would break every one of those citations. They are
    # a pointer, not the record — the record is the window-stamped file.
    for name, payload in rendered.items():
        stem, dot, ext = name.partition(".")
        _atomic(report_dir / f"{stem}-{window}{dot}{ext}", payload)
        _atomic(report_dir / name, payload)

    archive_dir = output_dir / "reports" / "archive" / run_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in rendered.items():
        _atomic(archive_dir / name, payload)
    return report_dir
