"""View 4 — Prompt archive. Addendum C.6.

Artifact table plus the HEALTH panel, which is R6a-R6g rendered live: a
prompt that fails cold start shows FAIL here instead of shipping silently.
"""
from __future__ import annotations

from pathlib import Path

from textual.containers import Vertical
from textual.widgets import DataTable, Static

from . import NavDataTable, _BaseView, fmt_ms, truncate


class PromptsView(_BaseView):
    # Same class of issue as DEFECT-4 (Findings): cap the table so the
    # HEALTH panel below it stays visible at a normal terminal height.
    DEFAULT_CSS = """
    PromptsView #prompts_table { height: 1fr; }
    """

    def compose(self):
        with Vertical():
            yield Static(self._render_header(), markup=False, id="prompts_body", classes="card")
            yield NavDataTable(id="prompts_table", zebra_stripes=True, cursor_type="row")
            yield Static(self._render_health(), markup=False, id="prompts_health", classes="card")

    def on_mount(self) -> None:
        table = self.query_one("#prompts_table", DataTable)
        table.add_columns("project", "artifact", "stable hash", "created")
        rows = 0
        for p in self.read_store.all_projects():
            for art in self.read_store.prompt_artifacts(p.id):
                table.add_row(
                    truncate(p.name, 28),
                    art.prompt_type,
                    art.stable_hash[:12],
                    fmt_ms(art.created_ms),
                )
                rows += 1
        if rows == 0:
            table.add_row("no prompts generated yet · press [r] to run now", "", "", "")

    def _render_header(self) -> str:
        total = sum(len(self.read_store.prompt_artifacts(p.id)) for p in self.read_store.all_projects())
        if total == 0:
            return "PROMPTS\n\nno prompts generated yet · press [r] to run now"
        return (
            f"PROMPTS\n\n{total} artifact(s) · [y] yank  [o] open in $EDITOR  "
            f"[h] harness variant  [enter] open"
        )

    def _render_health(self) -> str:
        """Live cold-start health per artifact on disk (R6a acceptance)."""
        out_dir = getattr(self.read_store, "_output_dir", None)
        if not out_dir:
            return "HEALTH\n\noutput directory unknown"
        reports = Path(out_dir) / "reports"
        if not reports.exists():
            return "HEALTH\n\nno generated artifacts on disk yet"
        try:
            from ...coldstart import check_tree

            summary = check_tree(reports)
        except Exception as exc:  # noqa: BLE001 — health panel never breaks the view
            return f"HEALTH\n\nunavailable: {exc}"
        verdict = summary.get("verdict", "?")
        lines = [
            "HEALTH  (R6a–R6g, checked live)",
            "",
            f"  cold start        {verdict}",
            f"  artifacts         {summary.get('artifact_count', 0)}",
            f"  citations checked {summary.get('checked_citations', 0)}",
            f"  broken citations  {summary.get('broken_citations', 0)}",
            f"  empty sections    {summary.get('empty_sections', 0)}",
            f"  skill refs (must be 0) {summary.get('unresolved_skill_refs', 0)}",
            f"  relative evidence refs (must be 0) {summary.get('relative_evidence_refs', 0)}",
        ]
        for failing in summary.get("failing", [])[:5]:
            lines.append(f"  FAIL {failing}")
        return "\n".join(lines)
