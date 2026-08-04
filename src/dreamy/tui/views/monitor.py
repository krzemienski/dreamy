"""View 6 — Live run monitor. Addendum C.8.

Pipeline stages plus a tailing log. Warnings render inline in amber and never
halt the pipeline — the visual grammar reinforces the bounded-warning
contract from R1.
"""
from __future__ import annotations

import time

from textual.containers import Vertical
from textual.widgets import DataTable, Static

from . import NavDataTable, _BaseView, fmt_duration, fmt_ms, truncate

_STAGES = ("ingest", "correlate", "analyze", "agent", "compiler", "report")


class MonitorView(_BaseView):
    def compose(self):
        with Vertical():
            yield Static(self._render_stages(), markup=False, id="monitor_body", classes="card")
            yield NavDataTable(id="monitor_log", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#monitor_log", DataTable)
        table.add_columns("time", "topic", "level", "message")
        since_ms = int(time.time() * 1000) - 24 * 3600 * 1000
        events = self.read_store.topic_events(
            since_ms,
            topics=["ingest", "correlate", "analyze", "agent", "research",
                    "teacher", "friends", "compiler", "report", "run"],
            limit=300,
        )
        if not events:
            # C.10: an idle monitor is a normal state with a defined rendering.
            table.add_row("", "", "", "no agent activity in the last 24h — press [r] on Runs to start")
            return
        for e in events[:120]:
            marker = "✦" if e.topic in ("research", "teacher", "friends", "compiler") else " "
            table.add_row(
                fmt_ms(e.ts_ms),
                f"{marker} {e.topic}",
                e.level,
                truncate(e.msg, 90),
            )

    def _render_stages(self) -> str:
        latest = self.read_store.latest_run()
        if latest is None:
            return "MONITOR\n\nno runs yet — press [r] on Runs to start"
        counts = latest.source_counts or {}
        lines = [
            f"MONITOR  run {latest.id}   started {fmt_ms(latest.started_ms)}"
            f"   {fmt_duration(latest.duration_ms)}",
            "",
        ]
        done = latest.status in ("ok", "failed")
        for stage in _STAGES:
            glyph = "●" if done else "◐"
            extra = ""
            if stage == "ingest" and counts:
                extra = f"  {sum(counts.values()):,} records from {len(counts)} source(s)"
            if stage == "compiler":
                extra = f"  {latest.prompt_count} artifact(s)"
            if stage == "analyze":
                extra = f"  {latest.finding_count} finding(s)"
            lines.append(f"  {glyph} {stage:<11}{extra}")
        lines.append("")
        lines.append(f"  status {latest.status}   agent spend ${latest.agent_cost_usd:.4f}")
        lines.append("  [f] follow  [a] abort run  [q] quit")
        return "\n".join(lines)
