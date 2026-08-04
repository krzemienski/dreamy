"""Cyberpunk theme tokens (addendum Part C.1)."""
from __future__ import annotations

TOKENS = {
    "bg": "#0A0A0F",
    "surface": "#12121A",
    "border": "#1F1F2E",
    "text": "#C8C8D4",
    "dim": "#6A6A80",
    "cyan": "#00F0FF",
    "magenta": "#FF2E97",
    "green": "#39FF14",
    "amber": "#FFB000",
    "red": "#FF3B47",
    "purple": "#A855F7",
}

STATUS_GLYPHS = {
    "complete": "●",
    "in_progress": "◐",
    "unverified": "◇",
    "abandoned": "○",
    "drift": "⤳",
    "critical": "▲",
}

REPO_READ_ONLY_GLYPH = "⬤ REPO: READ-ONLY"

DREAMY_CSS = """
Screen { background: #0A0A0F; color: #C8C8D4; }
Header { background: #12121A; color: #00F0FF; text-style: bold; }
Footer { background: #12121A; color: #6A6A80; }
Tabs { background: #0A0A0F; color: #C8C8D4; }
/* Textual's TabbedContent renders `Tabs > Tab`, not `TabBar > .tab--active`.
   The old selector matched nothing, so the active tab never underlined. */
Tabs > #tabs-list > Tab.-active { color: #00F0FF; text-style: underline; }
/* Chrome children are `Static`s, which default to width:100%. In a horizontal
   container that pushes every sibling after the first fully off-screen — the
   permanent READ-ONLY indicator (C.2) rendered at x=316 on a 160-col terminal
   and was never visible. `width: auto` sizes each to its own content.

   The x-axis fix alone was not sufficient. C.2 requires the indicator be
   "visible at all times", and an in-flow chrome row is not: when the
   composed content exceeds the viewport (virtual height 50 vs. 48 rows at
   160x48), the Screen scrolls and carries chrome off the top — measured
   live at `Region(y=-1)` with `scroll_offset=(0, 2)`, invisible in all six
   views. `dock: top` takes the row out of the scrollable flow so it holds
   its line regardless of content height or scroll position. */
#chrome { height: auto; dock: top; }
#chrome > Static { width: auto; height: 1; margin: 0 2 0 0; }
/* C.2: "a permanent green indicator ... visible at all times." Not a status
   that can change — it restates the read-only contract. */
#repo_flag { color: #39FF14; text-style: bold; }
#next_run { color: #6A6A80; }
.card { background: #12121A; border: solid #1F1F2E; padding: 1; margin: 1; }
/* Side panels need explicit widths or the second panel lands off-screen.
   Stack them under 100 columns rather than clipping content. */
#runs_panels { height: auto; }
#runs_panels > .card { width: 1fr; }
.narrow #runs_panels { layout: vertical; height: auto; }
.narrow #runs_panels > .card { width: 100%; }
#filters_lists { height: 1fr; }
#filters_lists > .card { width: 1fr; }
.narrow #filters_lists { layout: vertical; }
.narrow #filters_lists > .card { width: 100%; height: 1fr; }
#include_projects, #exclude_projects { height: 1fr; border: solid #1F1F2E; }
#save_project_filters { margin: 1; }
#dismiss_reason_box, #confirm_box { width: 64; height: auto; align: center middle; }
#dismiss_reasons { height: auto; margin: 1; }
.banner-amber { background: #FFB000; color: #0A0A0F; text-style: bold; }
.badge { padding: 0 1; }
.badge.complete { background: #39FF14; color: #0A0A0F; }
.badge.unverified { background: #FFB000; color: #0A0A0F; }
.badge.abandoned { background: #FF3B47; color: #0A0A0F; }
.badge.in-progress { background: #00F0FF; color: #0A0A0F; }
.badge.drift { background: #FF2E97; color: #0A0A0F; }
.muted { color: #6A6A80; }
"""
