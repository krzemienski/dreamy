"""View 2 — Findings browser. Addendum C.4.

Filter bar showing its own effect (63 → 9), a findings table carrying the
status glyph and the (D)/(A) provenance marker, and a detail pane.
"""
from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import DataTable, Static

from .. import theme as theme_mod
from . import NavDataTable, _BaseView, fmt_ms, truncate

_SEVERITY_GLYPH = {
    "high": theme_mod.STATUS_GLYPHS["critical"],
    "medium": theme_mod.STATUS_GLYPHS["in_progress"],
    "low": theme_mod.STATUS_GLYPHS["unverified"],
}


class FindingsView(_BaseView):
    """Findings list with view-only severity and lifecycle filters."""

    STATE_FILTERS: tuple[tuple[str, tuple[str, ...] | None], ...] = (
        ("new + regressed", ("new", "regressed")),
        ("all", None),
        ("persisting", ("persisting",)),
        ("resolved", ("resolved",)),
        ("dismissed", ("dismissed",)),
    )

    # DEFECT-4 fix: cap the table so the DETAIL panel below it stays visible
    # at a normal 50-row terminal instead of being pushed off-screen by
    # `height: auto` on a 100+ row table.
    DEFAULT_CSS = """
    FindingsView #findings_table { height: 1fr; }
    """

    def __init__(self, *, read_store, **kwargs):
        super().__init__(read_store=read_store, **kwargs)
        self.severity_filter: str | None = None
        self.state_filter_index = 0
        self.search_query: str = ""
        self._all_count = 0
        self._state_count = 0
        self._shown_count = 0
        self._visible_findings = []

    @property
    def state_filter(self) -> tuple[str, ...] | None:
        return self.STATE_FILTERS[self.state_filter_index][1]

    @property
    def state_filter_label(self) -> str:
        return self.STATE_FILTERS[self.state_filter_index][0]

    def compose(self):
        with Vertical():
            yield Static(self._render_filter_bar(), markup=False, id="findings_body", classes="card")
            yield NavDataTable(id="findings_table", zebra_stripes=True, cursor_type="row")
            yield Static(self._render_detail(), markup=False, id="findings_detail", classes="card")

    def on_mount(self) -> None:
        table = self.query_one("#findings_table", DataTable)
        table.add_columns("", "severity", "category", "title", "prov", "state", "first seen")
        # `compose` rendered the filter bar before any query had run, so its
        # counts were the initial zeros. Populating the table alone left that
        # header saying "0 total → 0 shown" above a table full of rows —
        # a count that contradicts the list beside it is worse than no count.
        self.refresh_view()

    def _rows(self):
        all_findings = self.read_store.findings()
        self._all_count = len(all_findings)
        filter_spec = {"state": list(self.state_filter)} if self.state_filter else None
        findings = self.read_store.findings(filter=filter_spec)
        self._state_count = len(findings)
        if self.severity_filter:
            findings = [finding for finding in findings if finding.severity == self.severity_filter]
        # DEFECT-2 fix: `/` search now really filters the list, by title,
        # case-insensitive substring match — the only searchable field the
        # C.4 wireframe shows in the row itself.
        if self.search_query:
            needle = self.search_query.lower()
            findings = [finding for finding in findings if needle in finding.title.lower()]
        self._shown_count = len(findings)
        self._visible_findings = findings
        return findings

    def selected_finding(self):
        table = self.query_one("#findings_table", DataTable)
        if not self._visible_findings or table.cursor_row >= len(self._visible_findings):
            return None
        return self._visible_findings[table.cursor_row]

    def refresh_view(self) -> None:
        table = self.query_one("#findings_table", DataTable)
        self._populate(table)
        self.query_one("#findings_body", Static).update(self._render_filter_bar())
        self.query_one("#findings_detail", Static).update(self._render_detail())

    def _populate(self, table: DataTable) -> None:
        table.clear()
        rows = self._rows()
        if not rows:
            reason = "0 match state/severity/search filters" if self.search_query else "0 match state/severity filters"
            table.add_row(
                "",
                "",
                "",
                f"{self._all_count} findings · {reason} · [f]/[v]/[/] adjust",
                "",
                "",
                "",
            )
            return
        for finding in rows[:200]:
            table.add_row(
                _SEVERITY_GLYPH.get(finding.severity, theme_mod.STATUS_GLYPHS["unverified"]),
                finding.severity,
                finding.category,
                truncate(finding.title, 60),
                f"({finding.provenance})",
                finding.delta_state,
                fmt_ms(finding.created_ms),
                key=finding.id,
            )

    def _render_filter_bar(self) -> str:
        severity = self.severity_filter or "all"
        search = f"  search:[{self.search_query}]" if self.search_query else ""
        return (
            f"FILTER severity:[{severity}]  category:[all]  state:[{self.state_filter_label}]{search}\n"
            f"{self._all_count} total → {self._state_count} state → {self._shown_count} shown"
            "   ·  [f] severity (view only)  [v] state  [/] search  [d] dismiss  [x] undismiss  [enter] detail"
        )

    def _render_detail(self) -> str:
        finding = self.selected_finding() if self.is_mounted else None
        if finding is None:
            finding = self._visible_findings[0] if self._visible_findings else None
        if finding is None:
            return "DETAIL\n\nno finding selected"
        return (
            "DETAIL\n\n"
            f"{finding.title}\n"
            f"severity={finding.severity}  category={finding.category}  confidence={finding.confidence}\n"
            f"state={finding.delta_state}"
            f"{f'  dismissal={finding.dismissal_reason}' if finding.dismissal_reason else ''}\n"
            f"provenance=({finding.provenance}) "
            f"{'deterministic — git + code evidence' if finding.provenance == 'D' else 'agent — model inference'}\n\n"
            f"{truncate(finding.detail, 400)}"
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "findings_table":
            self.query_one("#findings_detail", Static).update(self._render_detail())
