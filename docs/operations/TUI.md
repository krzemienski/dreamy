# TUI

Operator record for `dreamy tui` — the Textual-based interactive shell
(addendum C.1–C.11). Source of ground truth: direct read of
`src/dreamy/tui/app.py`, `src/dreamy/tui/theme.py`, and the live-tmux
fix verdict at
`~/.local/share/dreamy/acceptance/TUI-fix/focus-regression/FIX-VERDICT.txt`.

```bash
dreamy tui
```

Requires the `tui` extra (`textual==8.2.8`):

```bash
PIP_USER=0 /path/to/venv/bin/pip install --require-hashes -r config/requirements-tui.lock
```

## Views

Seven `TabbedContent` panes (`app.py::DreamyApp.compose`), each bound to a
number key:

| # | Binding | View | Widget |
|---|---|---|---|
| 1 | `1` | Runs | `RunsView` |
| 2 | `2` | Findings | `FindingsView` |
| 3 | `3` | Projects | `ProjectsView` |
| 4 | `4` | Prompts | `PromptsView` |
| 5 | `5` | Schedule | `ScheduleView` |
| 6 | `6` | Monitor | `MonitorView` |
| 7 | `7` | Next-run project filters | `ProjectFiltersView` |

Every view is constructed with `read_store=self.read_store` — the same
`ReadStore` read boundary the web dashboard uses; no SQL lives in `tui/`.

## Keybindings

Full binding table (`app.py::DreamyApp.BINDINGS`):

| Key | Action |
|---|---|
| `1`–`7` | Switch to the numbered view |
| `question_mark` (`?`) | Open keybinding help overlay |
| `q` | Quit |
| `r` | Run now (starts a real reconciliation run) |
| `f` | Cycle the findings severity filter |
| `v` | Cycle the findings state filter |
| `o` | Open editor at the selected artifact's path |
| `y` | Yank (copy) selected artifact path to clipboard |
| `h` | Show harness-variant notice (inline vs. portable, default inline) |
| `d` | Dismiss the selected finding — **requires confirmation + a reason** |
| `x` | Undismiss the selected finding — **requires confirmation** |
| `a` | Abort an in-flight run |
| `ctrl+s` | Save project include/exclude filters (View 7) |
| `i` | Install the launchd schedule — **requires confirmation** |
| `u` | Uninstall the launchd schedule — **requires confirmation** |
| `escape` | Back / close modal |
| `slash` (`/`) | Open live search (Findings view only) |

## Destructive-action confirmations

Every write-capable action routes through `ConfirmScreen`
(`app.py::ConfirmScreen`) before it executes: dismiss, undismiss, abort a
run, install, and uninstall the launchd schedule. `_confirm()` pushes the
modal and only calls the underlying handler if the user confirms. Dismiss
additionally requires an explicit reason via `DismissReasonScreen`, backed
by `ReadStore.DISMISSAL_REASONS` (the same four-code set the CLI's
`dreamy dismiss --reason` enforces) — there is no reason-less dismiss path
in either surface.

Confirmed live (tmux/PTY session, `~/.local/share/dreamy/acceptance/
TUI-fix/focus-regression/FIX-VERDICT.txt`, acceptance item 3): `d`
(dismiss) and `x` (undismiss) both target the **cursor row**, not row 0 —
verified by moving the cursor with `j`/`k` before invoking either action
and confirming the modal names the row the cursor was actually on.

## Focus handling (tab-switch fix)

Prior to a fix landed this run, switching tabs via number key or click
could leave no widget focused — two independent Textual-internal races
(a lazily-recomputed `Compositor.full_map` reading stale state, and
`Screen._reset_focus()`'s fallback search being DOM-local and unable to
reach across a `TabPane` boundary). Both are documented in the FIX-VERDICT
file above.

**Fix (`src/dreamy/tui/app.py`, `src/dreamy/tui/views/__init__.py`):**
focus ownership is centralized at the app level via
`DreamyApp.on_tabbed_content_tab_activated`, which resolves the newly
activated pane's primary `NavDataTable`/`NavSelectionList` and calls
`self.call_after_refresh(widget.focus)` — deferring the actual `.focus()`
call until after the current tick's layout/compositor pass settles. The
per-widget `on_show(self): self.focus()` methods that raced were removed.

**Verified live** across 19+ consecutive tab-switch hops in a real tmux
session (220×50, then resized to 90×50 and restored): the exact
regression scenario (1→2) and the full required sequence (1→2→4→7→1) both
showed a correctly positioned cursor-highlight in every pane, confirmed by
ANSI byte-diff. `j`/`k` cursor movement and `Enter`→detail-panel wiring
were verified in a freshly-focused pane, not just row 0. Zero unintended
writes to `state.db` or `config.json` occurred during the evidence
session (`state.db` mtime byte-identical before/after).

## Help overlay and search

- `?` opens `HelpScreen`, a real `screen_stack` push (depth 1→2), closes
  cleanly on `Esc`.
- `/` opens `SearchScreen`, a live-filter search input — **Findings is the
  only view with a searchable list today**; other views get a no-op toast
  per the current implementation's explicit fallback.

## Narrow-terminal layout

Under 100 columns (`NARROW_COLS = 100` in `app.py`), `_apply_layout` sets
the `narrow` CSS class, which stacks side-by-side panels vertically
(`theme.py::DREAMY_CSS`, `.narrow #runs_panels { layout: vertical; }` and
the equivalent rule for `#filters_lists`). Verified live at 90×50: the
Runs view's Sources/Attention panels correctly stack vertically instead of
side-by-side, and the `REPO: READ-ONLY` indicator moves to its own header
row. Restoring to 220×50 was confirmed structurally identical to the
original 220-column baseline (only the clock and transient cursor color
state differ, both expected).

## Read-only indicator

`AGENT-PIPELINE`/addendum C.2 requires a permanent, always-visible
indicator that Dreamy is read-only with respect to managed repositories.
`theme.py::REPO_READ_ONLY_GLYPH = "⬤ REPO: READ-ONLY"` is rendered by a
`Static` widget in the app's `#chrome` container — not a status that
changes, a restatement of the read-only contract that is always on
screen (or on its own header row when narrow-stacked).

## Theme

See `docs/operations/WEB-DASHBOARD.md`'s Theme section — the TUI and the
web dashboard share the same `theme.py::TOKENS` color palette and
`STATUS_GLYPHS` glyph set, so status meaning is consistent across both
surfaces.
