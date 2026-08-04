"""View 5 — Schedule manager. Addendum C.7.

Schedule mutation is the only state change this app makes outside the output
directory, so the exact launchctl command is shown BEFORE execution and both
actions require confirmation (R8).
"""
from __future__ import annotations

import os

from textual.containers import Vertical
from textual.widgets import Static

from . import _BaseView, fmt_ms


class ScheduleView(_BaseView):
    def compose(self):
        with Vertical():
            yield Static(self._render_job(), markup=False, id="schedule_body", classes="card")
            yield Static(self._render_actions(), markup=False, id="schedule_actions", classes="card")
            yield Static(self._render_raw(), markup=False, id="schedule_raw", classes="card")

    def _render_job(self) -> str:
        state = self.read_store.schedule_state()
        if state is None:
            return "LAUNCHD JOB\n\nschedule state unavailable"
        if not state.installed:
            # C.10: the only enabled action when the job is absent is install.
            return (
                "LAUNCHD JOB\n\n"
                f"  label     {state.label}\n"
                "  state     ○ not installed\n"
                "  plist     (absent)\n\n"
                "  [i] install is the only enabled action."
            )
        hours = state.interval_seconds / 3600 if state.interval_seconds else 0
        return (
            "LAUNCHD JOB\n\n"
            f"  label      {state.label}\n"
            f"  state      ● loaded · running\n"
            f"  interval   {state.interval_seconds}s ({hours:.0f}h)\n"
            f"  plist      {state.plist_path}\n"
            f"  last exit  {state.last_exit if state.last_exit is not None else '—'}\n"
            f"  next run   {fmt_ms(state.next_run_ms)}"
        )

    def _render_actions(self) -> str:
        uid = os.getuid()
        state = self.read_store.schedule_state()
        label = state.label if state else "com.nick.dreamy"
        plist = state.plist_path if state else ""
        return (
            "ACTIONS  (both require confirmation — this is the only write outside the output dir)\n\n"
            f"  [i] install / reinstall   launchctl bootstrap gui/{uid} {plist}\n"
            f"  [u] uninstall             launchctl bootout gui/{uid}/{label}\n"
            f"  [r] run now               dreamy run\n"
            f"  [s] refresh status\n\n"
            "  Uninstalling stops reconciliation until reinstalled. State, reports,\n"
            "  and prompts are KEPT."
        )

    def _render_raw(self) -> str:
        state = self.read_store.schedule_state()
        if state is None or not state.raw:
            return "launchctl print\n\n(no output — job not loaded)"
        return "launchctl print (live, never cached)\n\n" + state.raw[:1200]
