"""View 1 — Runs (home). Addendum C.3.

Layout: LAST RUN card · SOURCES panel · ATTENTION panel · HISTORY table.
No SQL here (ADR-006): every value arrives through ReadStore dataclasses.
"""
from __future__ import annotations

from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static

from .. import theme as theme_mod
from . import NavDataTable, _BaseView, fmt_duration, fmt_ms, fmt_usd


class RunsView(_BaseView):
    def compose(self):
        with Vertical():
            yield Static(self._render_last_run(), markup=False, id="runs_body", classes="card")
            with Horizontal(id="runs_panels"):
                yield Static(self._render_sources(), markup=False, id="runs_sources", classes="card")
                yield Static(self._render_attention(), markup=False, id="runs_attention", classes="card")
            table = NavDataTable(id="runs_history", zebra_stripes=True, cursor_type="row")
            yield table

    def on_mount(self) -> None:
        table = self.query_one("#runs_history", DataTable)
        table.add_columns("started", "status", "duration", "findings", "prompts", "cost")
        for run in self.read_store.runs_history(limit=20):
            table.add_row(
                fmt_ms(run.started_ms),
                f"{self._status_glyph(run.status)} {run.status}",
                fmt_duration(run.duration_ms),
                str(run.finding_count),
                str(run.prompt_count),
                fmt_usd(run.agent_cost_usd),
            )
        if table.row_count == 0:
            # C.10: specified empty state, never a bare empty table.
            table.add_columns() if not table.columns else None
            table.add_row("no runs yet · press [r] to run now", "", "", "", "", "")

    @staticmethod
    def _status_glyph(status: str) -> str:
        return {
            "ok": theme_mod.STATUS_GLYPHS["complete"],
            "running": theme_mod.STATUS_GLYPHS["in_progress"],
            "lock_held": theme_mod.STATUS_GLYPHS["unverified"],
            "failed": theme_mod.STATUS_GLYPHS["critical"],
        }.get(status, theme_mod.STATUS_GLYPHS["unverified"])

    def _render_last_run(self) -> str:
        latest = self.read_store.latest_run()
        if latest is None:
            return "LAST RUN\n\nno runs yet · press [r] to run now"
        projects = self.read_store.all_projects()
        lines = [
            "LAST RUN",
            "",
            f"{fmt_ms(latest.started_ms)}  {self._status_glyph(latest.status)} {latest.status}"
            f"   duration {fmt_duration(latest.duration_ms)}",
            f"projects {len(projects)}   findings {latest.finding_count}"
            f"   prompts {latest.prompt_count}",
            f"agent spend {fmt_usd(latest.agent_cost_usd)}",
        ]
        return "\n".join(lines)

    def _render_sources(self) -> str:
        stats = self.read_store.source_stats()
        if not stats:
            return "SOURCES\n\nno source counts recorded yet"
        lines = ["SOURCES", ""]
        for s in stats:
            if s.available:
                lines.append(f"  ● {s.source_id:<12} {s.record_count:>8,} records")
            else:
                # C.10: a missing harness is EXPECTED — dim '○ not installed',
                # never red. Colouring it red trains you to ignore red.
                lines.append(f"  ○ {s.source_id:<12} not installed / no records")
        return "\n".join(lines)

    def _render_attention(self) -> str:
        findings = self.read_store.findings()
        latest = self.read_store.latest_run()
        crit = [f for f in findings if f.severity == "high" and not f.resolved_ms]
        unverified = 0
        for p in self.read_store.all_projects():
            detail = self.read_store.project_detail(p.id)
            if detail:
                unverified += sum(
                    1 for e in detail.episodes if e.completion_status == "unverified"
                )
        lines = ["ATTENTION", ""]
        lines.append(f"  {theme_mod.STATUS_GLYPHS['critical']} {len(crit)} high-severity finding(s) open")
        lines.append(f"  {theme_mod.STATUS_GLYPHS['unverified']} {unverified} completion(s) unverified")
        if latest and latest.source_counts:
            absent = [k for k, v in latest.source_counts.items() if not v]
            if absent:
                lines.append(f"  ○ {len(absent)} source(s) contributed nothing: {', '.join(sorted(absent))}")
        if len(lines) == 2:
            lines.append("  nothing needs attention")
        return "\n".join(lines)
