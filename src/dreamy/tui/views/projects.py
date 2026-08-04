"""View 3 — Project detail / timeline. Addendum C.5.

Project list, then the selected project's intent episodes with their
completion glyphs, drift markers, and 30-day cost.
"""
from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import DataTable, Static

from .. import theme as theme_mod
from . import NavDataTable, _BaseView, fmt_ms, fmt_usd, truncate

_STATUS_GLYPH = {
    "complete": theme_mod.STATUS_GLYPHS["complete"],
    "in_progress": theme_mod.STATUS_GLYPHS["in_progress"],
    "unverified": theme_mod.STATUS_GLYPHS["unverified"],
    "abandoned": theme_mod.STATUS_GLYPHS["abandoned"],
}


class ProjectsView(_BaseView):
    # DEFECT-4 fix: cap the table so the TIMELINE panel below it stays
    # visible at a normal 50-row terminal instead of `height: auto` pushing
    # it off-screen once there are many projects.
    DEFAULT_CSS = """
    ProjectsView #projects_table { height: 1fr; }
    """

    def compose(self):
        with Vertical():
            yield Static(self._render_header(), markup=False, id="projects_body", classes="card")
            yield NavDataTable(id="projects_table", zebra_stripes=True, cursor_type="row")
            yield Static(self._render_timeline(), markup=False, id="projects_timeline", classes="card")

    def on_mount(self) -> None:
        table = self.query_one("#projects_table", DataTable)
        table.add_columns("project", "sessions", "first seen", "last seen", "30d cost", "per complete", "path")
        projects = self.read_store.all_projects()
        if not projects:
            table.add_row("no projects yet · press [r] to run now", "", "", "", "", "", "")
            return
        for p in projects:
            cost = self.read_store.cost_30d(p.id)
            table.add_row(
                truncate(p.name, 28),
                str(p.session_count),
                fmt_ms(p.first_seen_ms),
                fmt_ms(p.last_seen_ms),
                fmt_usd(cost.total_usd) if cost else "—",
                fmt_usd(cost.per_completed_intent_usd) if cost else "—",
                truncate(p.path, 48),
                key=p.id,
            )

    def _render_header(self) -> str:
        projects = self.read_store.all_projects()
        if not projects:
            return "PROJECTS\n\nno projects yet · press [r] to run now"
        rollup = self.read_store.cost_rollup()
        return (
            f"PROJECTS\n\n{len(projects)} project(s) correlated across harnesses "
            "· cost is display-only; never ranked/gated\n"
            f"attributed {fmt_usd(rollup['attributed_usd'])} · "
            f"unattributed {fmt_usd(rollup['unattributed_usd'])} "
            f"across {rollup['unattributed_requests']:,} request(s)"
        )

    def selected_project_id(self) -> str | None:
        table = self.query_one("#projects_table", DataTable)
        projects = self.read_store.all_projects()
        if not projects or table.cursor_row >= len(projects):
            return None
        return projects[table.cursor_row].id

    def _render_timeline(self) -> str:
        projects = self.read_store.all_projects()
        if not projects:
            return "TIMELINE\n\n(no project selected)"
        project_id = self.selected_project_id() if self.is_mounted else projects[0].id
        detail = self.read_store.project_detail(project_id or projects[0].id)
        if detail is None:
            return "TIMELINE\n\n(project detail unavailable)"
        lines = [f"INTENT EPISODES — {detail.summary.name}", ""]
        if not detail.episodes:
            lines.append("  no intent episodes reconstructed in this window")
        for episode in detail.episodes[:12]:
            glyph = _STATUS_GLYPH.get(episode.completion_status, theme_mod.STATUS_GLYPHS["unverified"])
            drift = (
                f"  {theme_mod.STATUS_GLYPHS['drift']} {episode.drift_type}"
                if episode.drift_type
                else ""
            )
            lines.append(
                f"  {glyph} {episode.completion_status:<12} "
                f"{fmt_ms(episode.started_ms)} → {fmt_ms(episode.ended_ms)}{drift}"
            )
            lines.append(f"      “{truncate(episode.original_intent, 96)}”")
        if detail.cost_30d:
            lines.append("")
            lines.append(
                f"  30d cost {fmt_usd(detail.cost_30d.total_usd)} "
                f"· {fmt_usd(detail.cost_30d.per_completed_intent_usd)} per completed intent "
                f"· link confidence mix {detail.cost_30d.confidence_mix}"
            )
        return "\n".join(lines)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "projects_table":
            self.query_one("#projects_timeline", Static).update(self._render_timeline())

