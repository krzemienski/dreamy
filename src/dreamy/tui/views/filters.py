"""View 7 — next-run project selection. Addendum R12.

The two lists are run configuration, not filters on the current display.
"""
from __future__ import annotations

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, SelectionList, Static

from ...config import Config, load_config, save_config
from . import NavSelectionList, _BaseView


class ProjectFiltersView(_BaseView):
    """Edit include/exclude project sets for the next reconciliation run."""

    def __init__(self, *, read_store, **kwargs):
        super().__init__(read_store=read_store, **kwargs)
        self._load_error: str | None = None
        # Genuinely optional: a malformed config must not stop the TUI from
        # opening — `compose` renders an error card instead, and the save
        # button is withheld. Annotated so the None branch is a declared
        # state rather than one mypy only sees under --check-untyped-defs.
        self.cfg: Config | None
        try:
            self.cfg = load_config()
        except ValueError as exc:
            self.cfg = None
            self._load_error = str(exc)

    def compose(self):
        with Vertical():
            yield Static(self._render_header(), markup=False, id="filters_body", classes="card")
            if self.cfg is None:
                yield Static(
                    f"Configuration unavailable: {self._load_error}\nNo changes can be saved.",
                    markup=False,
                    id="filters_error",
                    classes="card",
                )
            else:
                projects = self.read_store.all_projects()
                include = set(self.cfg.include_projects)
                exclude = set(self.cfg.exclude_projects)
                with Horizontal(id="filters_lists"):
                    with Vertical(classes="card"):
                        yield Static("INCLUDE — only selected projects", markup=False)
                        yield NavSelectionList(
                            *((f"● {project.name}", project.path, project.path in include) for project in projects),
                            id="include_projects",
                        )
                    with Vertical(classes="card"):
                        yield Static("EXCLUDE — selected projects are skipped", markup=False)
                        yield NavSelectionList(
                            *((f"○ {project.name}", project.path, project.path in exclude) for project in projects),
                            id="exclude_projects",
                        )
                if not projects:
                    yield Static(
                        "No projects discovered yet. Run Dreamy once, then return here.",
                        markup=False,
                        id="filters_empty",
                        classes="card",
                    )
                yield Button("Save for next run", id="save_project_filters", variant="primary")

    def _render_header(self) -> str:
        if self.cfg is None:
            return "NEXT-RUN PROJECTS\n\nconfiguration is invalid or unreadable"
        return (
            "NEXT-RUN PROJECTS — this does NOT filter any current view\n\n"
            f"include {len(self.cfg.include_projects)} · exclude {len(self.cfg.exclude_projects)}\n"
            "Space toggles. A project cannot be both included and excluded. [ctrl+s] saves atomically."
        )

    def save(self) -> bool:
        if self.cfg is None:
            self.app.notify("project filters unavailable: config did not load", severity="error")
            return False
        include = self.query_one("#include_projects", SelectionList).selected
        exclude = self.query_one("#exclude_projects", SelectionList).selected
        overlap = sorted(set(include) & set(exclude))
        if overlap:
            self.app.notify(
                f"not saved: {len(overlap)} project(s) selected in both include and exclude",
                severity="error",
            )
            return False
        self.cfg.include_projects = list(include)
        self.cfg.exclude_projects = list(exclude)
        try:
            path = save_config(self.cfg)
        except ValueError as exc:
            self.app.notify(f"project filters rejected: {exc}", severity="error")
            return False
        self.query_one("#filters_body", Static).update(self._render_header())
        self.app.notify(f"saved for NEXT run: {path}")
        return True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save_project_filters":
            self.save()
