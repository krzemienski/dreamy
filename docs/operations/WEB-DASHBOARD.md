# WEB-DASHBOARD

Design record for `dreamy web` (`R17`) — the read-only loopback dashboard.
Source of ground truth: direct read of `src/dreamy/web/*` and
`src/dreamy/tui/theme.py`, cross-checked against Phase 4's 24-cell matrix
evidence at `~/.local/share/dreamy/acceptance/dashboard-http/` and
`~/.local/share/dreamy/acceptance/dashboard-visual/`. This document
records design, not a second copy of that evidence — see the acceptance
paths above for the raw matrix output.

## What this is

A second *view* of the same state Dreamy already persists, never a second
source of truth and never a write path. Schedule mutation, finding
dismissal, and run-now stay in the CLI and TUI, where an operator
confirmation is enforced before anything happens. The dashboard's job is
strictly to render `ReadStore` output through a browser.

## Starting it

```bash
dreamy web --host 127.0.0.1 --port 8765
```

`--host` defaults to `127.0.0.1`. A non-loopback value is rejected
**before** the socket opens — `_loopback()` in `server.py` resolves the
hostname via `getaddrinfo` and raises `ValueError` unless every resolved
address is a loopback address; `build_server()` calls it before
constructing `ThreadingHTTPServer`. There is no warn-after-the-fact path.

## Routes

Nine routes exist under `src/dreamy/web/api.py::route()` (verified by
direct read, not by counting the nav bar — the nav bar under-counts by
one, see "Six views vs. nine routes" below):

| Path | Purpose |
|---|---|
| `GET /healthz` | Liveness probe: `{"status": "ok", "has_state": true}` |
| `GET /api/v1/overview` | Latest run, per-source stats, schedule state, total agent spend, project/finding counts |
| `GET /api/v1/runs?limit=N` | Run history (`limit` clamped to 1–200) |
| `GET /api/v1/findings?project=&severity=&category=&state=` | Findings with R13 delta state; default `state` filter is `new,regressed` unless `state=all` |
| `GET /api/v1/projects` | Project list |
| `GET /api/v1/projects/<id>` | One project's detail (404 if not found) |
| `GET /api/v1/prompts?project=&type=` | Compiled prompt artifacts |
| `GET /api/v1/schedule` | Schedule state |
| `GET /api/v1/monitor?limit=N` | Live stage/log event stream |

`dispatch()` gates method before routing: anything other than `GET`/`HEAD`
returns `405` with an `Allow: GET, HEAD` header and a JSON error body
carrying no stack trace or absolute path — `server.py`'s
`_method_not_allowed` handler.

## Security posture

- **Loopback-only.** Enforced in `_loopback()` before the server binds
  (see above).
- **Read-only, per-request store.** `server.py`'s request handler opens
  and closes its own `ReadStore` per request inside a `try/finally`; no
  SQLite connection is shared across requests or threads.
- **No SQL in `web/`.** `api.py` calls only `ReadStore` methods —
  `latest_run`, `source_stats`, `schedule_state`, `total_agent_spend`,
  `all_projects`, `findings`, `runs_history`, `project_detail`,
  `prompt_artifacts`, `topic_events` — ADR-006's boundary.
- **Content-Security-Policy.** Every response carries:
  `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'
  data:; connect-src 'self'; font-src 'self'; object-src 'none';
  base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
  (`server.py:_CSP`). `img-src` allows `data:` only for the inlined
  favicon.
- **Zero external network requests.** Static assets (`server.py::
  _static_root()`) are served from the installed package via
  `importlib.resources` — no CDN, no font fetch, no build step.

## Theme

`src/dreamy/tui/theme.py::TOKENS` defines the token set the dashboard's
CSS is derived from (a "dark hybrid" palette — dark background with
saturated accent colors for status):

| Token | Hex | Semantic use |
|---|---|---|
| `bg` | `#0A0A0F` | App background |
| `surface` | `#12121A` | Panels, table headers |
| `border` | `#1F1F2E` | Rules, frame lines |
| `text` | `#C8C8D4` | Body text |
| `dim` | `#6A6A80` | Metadata, timestamps, secondary text |
| `cyan` | `#00F0FF` | Primary accent, focus ring, in-progress |
| `magenta` | `#FF2E97` | Selection, drift marker |
| `green` | `#39FF14` | Complete, PASS, healthy |
| `amber` | `#FFB000` | Unverified, warning |
| `red` | `#FF3B47` | Abandoned, FAIL, critical |
| `purple` | `#A855F7` | Agent-generated content marker |

`STATUS_GLYPHS` (also `theme.py`) pairs status states with glyphs so color
is never the only signal: `●` complete, `◐` in-progress, `◇` unverified,
`○` abandoned, `⤳` drift, `▲` critical.

## Six views vs. nine routes

The T3 scratchpad flagged a real distinction that this document preserves
rather than papers over: `R17g` names six views — **overview, projects,
findings, project detail, prompts, monitor** — but the shipped nav bar
(`static/index.html`) surfaces **Runs, Findings, Projects, Prompts,
Schedule, Monitor**, and "project detail" is a hash route
(`#/project/<id>`), not a nav tab. "Schedule" is a seventh nav surface not
named in `R17g`. Presence must be judged against `R17g`'s list, not the
nav bar's label set.

## Read layer

Every response is built from `read.ReadStore` (`src/dreamy/read.py`). No
route computes derived state independently of what `ReadStore` already
exposes — the dashboard has no logic `ReadStore` does not already own.

## What Phase 4 verified end-to-end

Phase 4's 24-cell matrix (six views × four states: populated, loading,
empty, unavailable) was verified against a running `dreamy web` instance
to this exact depth: **populated states observed live for all six views;
empty states observed live for two** (findings with an impossible filter,
prompts with no project param); **loading and unavailable states
code-verified only** (loading is a client-side JS timing state not
observable via HTTP evidence, and the 503 unavailable path was not
deliberately triggered against the live production DB). The R17
mechanical checks (loopback rejection, 405+`Allow` on non-GET,
per-request store isolation, zero external network requests, error
sanitization, and server health across a live pipeline run — the server
stayed up and healthy while a real run wrote `state.db`; the spaced
verification fetches landed just after the write completed) were
all exercised live.
Raw output lives at `~/.local/share/dreamy/acceptance/dashboard-http/`
and `~/.local/share/dreamy/acceptance/dashboard-visual/` — this document
cross-references that evidence rather than duplicating the 24-cell table
here.
