"""Shared view base and formatting helpers.

ADR-006 boundary: widget modules contain no database imports or SQL. Every value
reaches a widget as a plain dataclass from `read.ReadStore`.
"""
from __future__ import annotations

from datetime import UTC, datetime

from textual.binding import Binding
from textual.containers import Container
from textual.widgets import DataTable, SelectionList

from ...read import ReadStore


class _BaseView(Container):
    """Shared base for dreamy views. Reads only through ReadStore."""

    def __init__(self, *, read_store: ReadStore, **kwargs):
        super().__init__(**kwargs)
        self.read_store = read_store


class NavDataTable(DataTable):
    """j/k navigate rows (Textual only binds up/down natively). Focus is
    NOT handled here — see `DreamyApp.on_tabbed_content_tab_activated`.
    A per-widget `on_show(self): self.focus()` looked correct in isolation
    but is unreliable across a real tab-switch sequence: Textual's
    Compositor.full_map is a lazily-recomputed property with a side effect
    — if anything reads widget geometry (e.g. a stray `.refresh()`) before
    Screen._refresh_layout's official `reflow()` call in the same tick, the
    lazy recompute silently overwrites `_full_map` early, so the official
    reflow's `shown_widgets = new_widgets - old_widgets` diff comes back
    empty and `Show` never posts to this widget on some tab switches.
    Separately, `Widget._on_hide` -> `blur()` -> `Screen._reset_focus`'s
    fallback only searches the outgoing widget's DOM siblings, which can
    never reach across a TabPane boundary — so even a race-free `on_show`
    would still lose to the synchronous focus-reset-to-None on the OLD
    tab's hide. Both are Textual internals outside this widget's control;
    the app-level TabActivated handler (which fires reliably on every real
    switch, including the very first) is the only sound fix."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]


class NavSelectionList(SelectionList):
    """j/k + row-nav treatment for the Filters view's include/exclude lists
    (R12). See `NavDataTable`'s docstring — focus-on-tab-switch is handled
    centrally by `DreamyApp.on_tabbed_content_tab_activated`, not here."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]


def fmt_ms(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def fmt_duration(ms: int | None) -> str:
    if not ms or ms < 0:
        return "—"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes}m{sec:02d}s"


def fmt_usd(value: float | None) -> str:
    return f"${(value or 0.0):.4f}"


def truncate(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


__all__ = [
    "_BaseView",
    "NavDataTable",
    "NavSelectionList",
    "fmt_ms",
    "fmt_duration",
    "fmt_usd",
    "truncate",
]
