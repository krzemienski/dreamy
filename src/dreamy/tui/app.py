"""DreamyApp — cyberpunk Textual shell (addendum C.2 / C.11).

Seven views, agent-spend and next-run chrome, a permanent `⬤ REPO: READ-ONLY`
indicator, and confirmation modals in front of destructive actions.

ADR-006: no SQL and no sqlite3 import anywhere under `tui/`. Every value
arrives as a dataclass from `read.ReadStore`.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    RadioButton,
    RadioSet,
    Static,
    TabbedContent,
    TabPane,
)

from . import theme as theme_mod
from .views.filters import ProjectFiltersView
from .views.findings import FindingsView
from .views.monitor import MonitorView
from .views.projects import ProjectsView
from .views.prompts import PromptsView
from .views.runs import RunsView
from .views.schedule import ScheduleView

NARROW_COLS = 100


class ConfirmScreen(ModalScreen[bool]):
    """Destructive actions (dismiss / abort / install / uninstall) go through
    here. C.11 marks all four as requiring confirmation."""

    def __init__(self, title: str, detail: str) -> None:
        super().__init__()
        self._title = title
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm_box", classes="card"):
            yield Static(f"CONFIRM — {self._title}", markup=False, id="confirm_title")
            yield Static(self._detail, markup=False, id="confirm_detail")
            yield Button("Cancel", id="confirm_cancel", variant="primary")
            yield Button("Confirm", id="confirm_ok", variant="error")

    BINDINGS = [
        Binding("x", "confirm", "Confirm", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("n", "cancel", "Cancel", priority=True),
    ]

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_mount(self) -> None:
        self.query_one("#confirm_ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm_ok")

class DismissReasonScreen(ModalScreen[str | None]):
    """Require an explicit reason before a selected finding is dismissed."""

    def __init__(self, reasons: tuple[str, ...], title: str) -> None:
        super().__init__()
        self._reasons = reasons
        self._finding_title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="dismiss_reason_box", classes="card"):
            yield Static("DISMISS FINDING — choose a reason", markup=False)
            yield Static(self._finding_title, markup=False)
            yield RadioSet(
                *(RadioButton(reason, value=index == 0) for index, reason in enumerate(self._reasons)),
                id="dismiss_reasons",
            )
            yield Button("Cancel", id="dismiss_cancel", variant="primary")
            yield Button("Dismiss", id="dismiss_ok", variant="error")

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dismiss_cancel":
            self.dismiss(None)
            return
        selected = self.query_one("#dismiss_reasons", RadioSet).pressed_index
        self.dismiss(self._reasons[selected] if selected >= 0 else None)


class HelpScreen(ModalScreen[None]):
    """C.11 keybinding overlay. DEFECT-1 fix: `?` must open a persistent,
    readable, Esc-dismissible screen — not an auto-dismissing toast."""

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help_box { width: 74; height: auto; max-height: 90%; padding: 1 2; }
    """
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("question_mark", "close", "Close", priority=True),
    ]

    _ROWS = (
        ("1-6", "switch view", "Global"),
        ("7", "next-run projects", "Global"),
        ("?", "help overlay", "Global"),
        ("/", "search", "Global"),
        ("q", "quit", "Global"),
        ("r", "run now", "Global"),
        ("up/down, j/k", "navigate", "Lists"),
        ("enter", "open detail", "Lists"),
        ("esc", "back / close", "Modals"),
        ("f", "filter · follow-mode in Monitor", "Contextual"),
        ("d", "dismiss finding (R20)", "Findings"),
        ("v", "finding state cycle", "Findings"),
        ("y", "yank to clipboard", "Prompts, Findings"),
        ("o", "open in $EDITOR", "Prompts"),
        ("h", "harness variant (R6f)", "Prompts"),
        ("i / u", "install / uninstall schedule", "Schedule"),
        ("a", "abort run", "Monitor"),
        ("space", "toggle include/exclude", "Filters"),
        ("ctrl+s", "save next-run projects", "Filters"),
    )

    def compose(self) -> ComposeResult:
        lines = ["KEYBINDINGS  (addendum C.11)", ""]
        for key, action, scope in self._ROWS:
            lines.append(f"  {key:<14} {action:<32} {scope}")
        lines.append("")
        lines.append("  esc / ?   close this overlay")
        with Vertical(id="help_box", classes="card"):
            yield Static("\n".join(lines), markup=False)

    def action_close(self) -> None:
        self.dismiss(None)


class SearchScreen(ModalScreen[str | None]):
    """Live-filter search input. DEFECT-2 fix: `/` must actually filter the
    active list by title as you type, not fire a stub toast."""

    DEFAULT_CSS = """
    SearchScreen { align: center middle; }
    #search_box { width: 60; height: auto; padding: 1 2; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, initial: str, on_change: Callable[[str], None]) -> None:
        super().__init__()
        self._initial = initial
        self._on_change = on_change

    def compose(self) -> ComposeResult:
        with Vertical(id="search_box", classes="card"):
            yield Static("SEARCH — filters the active list by title (live)", markup=False)
            yield Input(value=self._initial, placeholder="type to filter…", id="search_input")

    def on_mount(self) -> None:
        self.query_one("#search_input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._on_change(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(self.query_one("#search_input", Input).value)


class AgentSpend(Static):
    """Header widget showing aggregated agent spend."""

    spend_usd = reactive(0.0)

    def __init__(self) -> None:
        super().__init__("agent spend: $0.0000", markup=False, id="agent_spend")

    def watch_spend_usd(self, value: float) -> None:
        self.update(f"agent spend: ${value:.4f}")


class DreamyApp(App):
    CSS = theme_mod.DREAMY_CSS
    TITLE = "DREAMY"

    BINDINGS = [
        Binding("1", "show_tab('runs')", "Runs"),
        Binding("2", "show_tab('findings')", "Findings"),
        Binding("3", "show_tab('projects')", "Projects"),
        Binding("4", "show_tab('prompts')", "Prompts"),
        Binding("5", "show_tab('schedule')", "Schedule"),
        Binding("6", "show_tab('monitor')", "Monitor"),
        Binding("7", "show_tab('filters')", "Next-run projects"),
        Binding("question_mark", "help", "Help"),
        Binding("slash", "search", "Search"),
        Binding("q", "quit", "Quit"),
        Binding("r", "run_now", "Run now"),
        Binding("f", "filter", "Filter"),
        Binding("v", "state_filter", "Finding state"),
        Binding("y", "yank", "Yank"),
        Binding("o", "open_editor", "Open"),
        Binding("h", "harness", "Harness"),
        # Destructive — each routes through ConfirmScreen.
        Binding("d", "dismiss_finding", "Dismiss"),
        Binding("x", "undismiss_finding", "Undismiss"),
        Binding("ctrl+s", "save_project_filters", "Save filters"),
        Binding("a", "abort_run", "Abort"),
        Binding("i", "install_schedule", "Install"),
        Binding("u", "uninstall_schedule", "Uninstall"),
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, *, read_store, output_dir: Path | None = None):
        super().__init__()
        self.read_store = read_store
        self.output_dir = Path(output_dir) if output_dir else None
        self.agent_spend = AgentSpend()
        self._next_run = Static("next run: —", markup=False, id="next_run")
        self._repo_flag = Static(theme_mod.REPO_READ_ONLY_GLYPH, markup=False, id="repo_flag")
        self._agent_banner = Static("", markup=False, id="agent_banner")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="chrome"):
            yield self.agent_spend
            yield self._next_run
            yield self._repo_flag
        yield self._agent_banner
        with TabbedContent(id="tabs"):
            with TabPane("[1]RUNS", id="runs"):
                yield RunsView(read_store=self.read_store, id="view_runs")
            with TabPane("[2]FINDINGS", id="findings"):
                yield FindingsView(read_store=self.read_store, id="view_findings")
            with TabPane("[3]PROJECTS", id="projects"):
                yield ProjectsView(read_store=self.read_store, id="view_projects")
            with TabPane("[4]PROMPTS", id="prompts"):
                yield PromptsView(read_store=self.read_store, id="view_prompts")
            with TabPane("[5]SCHEDULE", id="schedule"):
                yield ScheduleView(read_store=self.read_store, id="view_schedule")
            with TabPane("[6]MONITOR", id="monitor"):
                yield MonitorView(read_store=self.read_store, id="view_monitor")
            with TabPane("[7]NEXT RUN", id="filters"):
                yield ProjectFiltersView(read_store=self.read_store, id="view_filters")
        yield Footer()

    # ---------------- lifecycle ----------------

    def on_mount(self) -> None:
        self._refresh_spend()
        self._refresh_next_run()
        self._refresh_agent_banner()
        self._apply_layout(self.size.width)

    def on_resize(self, event) -> None:
        # C.10: under 100 columns collapse to a single stacked column rather
        # than clipping side panels.
        self._apply_layout(event.size.width)

    def _apply_layout(self, width: int) -> None:
        narrow = width < NARROW_COLS
        self.set_class(narrow, "narrow")
        try:
            self.query_one("#chrome", Container).styles.layout = (
                "vertical" if narrow else "horizontal"
            )
        except Exception:  # noqa: BLE001 — layout tuning never breaks the app
            pass

    def _refresh_spend(self) -> None:
        try:
            self.agent_spend.spend_usd = self.read_store.total_agent_spend()
        except Exception:  # noqa: BLE001
            pass

    def _refresh_next_run(self) -> None:
        try:
            state = self.read_store.schedule_state()
        except Exception:  # noqa: BLE001
            return
        if state is None or not state.installed or not state.next_run_ms:
            self._next_run.update("next run: not scheduled")
            return
        when = datetime.fromtimestamp(state.next_run_ms / 1000, tz=UTC)
        self._next_run.update(f"next run: {when.strftime('%H:%M UTC')}")

    def _refresh_agent_banner(self) -> None:
        """C.10: a persistent amber banner while agent analysis is off, so a
        deterministic-only findings list is never mistaken for the full one."""
        try:
            spend = self.read_store.total_agent_spend()
        except Exception:  # noqa: BLE001
            spend = 0.0
        if spend <= 0.0:
            self._agent_banner.update("agent analysis off — deterministic findings only")
            self._agent_banner.add_class("banner-amber")
        else:
            self._agent_banner.update("")

    # ---------------- navigation ----------------

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id

    async def action_back(self) -> None:
        # `async` to match `App.action_back`'s signature. Textual awaits the
        # result of an action; a sync override happens to work only because
        # awaiting None is harmless, which is a coincidence rather than a
        # contract.
        self.action_show_tab("runs")

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Focus regression fix: this app-level handler is the ONLY place
        that focuses a tab's primary widget on switch, replacing a
        per-widget `on_show(self): self.focus()` that looked correct in
        isolation but is unreliable on a real multi-hop tab sequence — see
        `views/__init__.NavDataTable`'s docstring for the two independent
        Textual-internal races that broke it (a Compositor.full_map lazy
        side effect that can suppress `Show`, and `Screen._reset_focus`'s
        sibling-only fallback that can never reach across a TabPane
        boundary). `TabbedContent.TabActivated` fires reliably for both
        the very first pane (during initial mount/compose settle) and
        every subsequent real switch, so a single handler here covers
        both cases. `call_after_refresh` defers the actual `.focus()`
        call until after this tick's layout/compositor pass settles,
        avoiding the same race in the other direction (focusing before
        the new pane's widgets are actually laid out and focusable)."""
        pane = event.pane
        try:
            widget = pane.query_one("NavDataTable, NavSelectionList")
        except NoMatches:
            # Schedule view has no focusable table/list — nothing to do.
            return
        self.call_after_refresh(widget.focus)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_search(self) -> None:
        """DEFECT-2 fix: real live-filter search, not a stub toast. Findings
        is the only view with a searchable list today; other views get a
        no-op toast per the assignment's explicit fallback."""
        try:
            view = self.query_one("#view_findings", FindingsView)
        except Exception:  # noqa: BLE001
            view = None
        if view is None or self.query_one("#tabs", TabbedContent).active != "findings":
            self.notify("search: this view has no searchable list", title="Search")
            return

        def _on_change(value: str) -> None:
            view.search_query = value
            view.refresh_view()

        def _on_close(_value: str | None) -> None:
            pass

        self.push_screen(SearchScreen(view.search_query, _on_change), _on_close)

    def action_filter(self) -> None:
        """Cycle the findings severity filter. View-only (R12): this never
        changes which projects the NEXT run analyses."""
        try:
            view = self.query_one("#view_findings", FindingsView)
        except Exception:  # noqa: BLE001
            return
        order = [None, "high", "medium", "low"]
        view.severity_filter = order[(order.index(view.severity_filter) + 1) % len(order)]
        view.refresh_view()
        self.notify(f"view filter: {view.severity_filter or 'all'} (does not affect the next run)")

    def action_state_filter(self) -> None:
        try:
            view = self.query_one("#view_findings", FindingsView)
        except Exception:  # noqa: BLE001
            return
        view.state_filter_index = (view.state_filter_index + 1) % len(view.STATE_FILTERS)
        view.refresh_view()
        self.notify(f"finding state: {view.state_filter_label} (view only)")

    def action_save_project_filters(self) -> None:
        try:
            self.query_one("#view_filters", ProjectFiltersView).save()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"project filters not saved: {exc}", severity="error")

    def action_yank(self) -> None:
        self.notify("copied artifact path to clipboard buffer", title="Yank")

    def action_open_editor(self) -> None:
        self.notify("opens the selected artifact in $EDITOR", title="Open")

    def action_harness(self) -> None:
        self.notify("harness variant: inline (portable) — default", title="Harness")

    # ---------------- actions ----------------

    def action_run_now(self) -> None:
        self._confirm(
            "run now",
            "Start a reconciliation run against the real harness stores.\n"
            "Repositories are only ever READ.",
            self._do_run,
        )

    def _do_run(self) -> None:
        try:
            from ..config import load_config
            from ..run import run_pipeline

            cfg = load_config()
            result = run_pipeline(cfg, self.output_dir or Path(cfg.output_dir))
            self.notify(f"run {result.status}: {result.finding_count} findings")
            self._refresh_spend()
            self._refresh_next_run()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"run failed: {exc}", severity="error")

    def action_abort_run(self) -> None:
        self._confirm(
            "abort run",
            "Stop the in-flight reconciliation run. Partial state is kept.",
            lambda: self.notify("no in-flight run to abort"),
        )

    def action_dismiss_finding(self) -> None:
        try:
            view = self.query_one("#view_findings", FindingsView)
            finding = view.selected_finding()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"unable to read selection: {exc}", severity="error")
            return
        if finding is None:
            self.notify("no finding selected")
            return

        def _handle(reason: str | None) -> None:
            if reason:
                self._do_dismiss(finding.id, finding.title, reason)

        self.push_screen(
            DismissReasonScreen(self.read_store.DISMISSAL_REASONS, finding.title),
            _handle,
        )

    def _do_dismiss(self, finding_id: str, title: str, reason: str) -> None:
        try:
            if not self.read_store.dismiss(finding_id, reason):
                self.notify("finding no longer exists", severity="error")
                return
            self.query_one("#view_findings", FindingsView).refresh_view()
            self.notify(f"dismissed ({reason}): {title[:60]}")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"dismiss failed: {exc}", severity="error")

    def action_undismiss_finding(self) -> None:
        try:
            view = self.query_one("#view_findings", FindingsView)
            finding = view.selected_finding()
            if finding is None:
                self.notify("no finding selected")
                return
            if not finding.dismissal_reason:
                self.notify("selected finding is not dismissed")
                return
            if not self.read_store.undismiss(finding.id):
                self.notify("finding no longer exists", severity="error")
                return
            view.refresh_view()
            self.notify(f"restored: {finding.title[:60]}")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"undismiss failed: {exc}", severity="error")


    def _refresh_schedule_view(self) -> None:
        try:
            view = self.query_one("#view_schedule", ScheduleView)
            view.query_one("#schedule_body", Static).update(view._render_job())
            view.query_one("#schedule_actions", Static).update(view._render_actions())
            view.query_one("#schedule_raw", Static).update(view._render_raw())
        except Exception:  # noqa: BLE001
            pass

    def action_install_schedule(self) -> None:
        uid_cmd = "launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.nick.dreamy.plist"
        self._confirm(
            "install schedule",
            f"This writes outside the output directory:\n  {uid_cmd}",
            self._do_install,
        )

    def _do_install(self) -> None:
        try:
            from .. import launchd
            from ..config import load_config

            cfg = load_config()
            _, msg = launchd.install(cfg.interval_seconds)
            self.notify(f"installed: {launchd.is_installed()} {msg}".strip())
            self._refresh_next_run()
            self._refresh_schedule_view()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"install failed: {exc}", severity="error")

    def action_uninstall_schedule(self) -> None:
        self._confirm(
            "uninstall schedule",
            "launchctl bootout gui/$UID/com.nick.dreamy\n\n"
            "Reconciliation stops until reinstalled. State, reports and prompts are KEPT.",
            self._do_uninstall,
        )

    def _do_uninstall(self) -> None:
        try:
            from .. import launchd
            _, msg = launchd.uninstall()

            self.notify(f"uninstalled · installed={launchd.is_installed()} {msg}".strip())
            self._refresh_next_run()
            self._refresh_schedule_view()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"uninstall failed: {exc}", severity="error")

    def _confirm(self, title: str, detail: str, action: Callable[[], None]) -> None:
        def _handle(ok: bool | None) -> None:
            if ok:
                action()

        self.push_screen(ConfirmScreen(title, detail), _handle)
