# dreamy — Command Usage Guide

`dreamy` is a read-only cross-harness coding-session reconciler. It ingests
coding sessions from six sources (`claude`, `codex`, `nine_router`, `omp`,
`opencode`, `pi`), correlates them into per-project timelines, and compiles
evidence-cited prompt artifacts.

Global invocation:

```
dreamy [--config CONFIG] <command> ...
```

`--config CONFIG` — path to `config.json` (default: `~/.local/share/dreamy/config.json`).
Missing file returns default settings; a malformed one is a hard error before
any subcommand runs.

**Dispatch order (applies to every command except `verify-run`):** `main()`
first checks that the `git` binary is on `PATH` — missing it exits `127`
before anything else runs — then loads `--config` via `load_config()`. A
`config.json` that fails to parse (`ValueError`) exits `2` with
`dreamy: <message>` printed to stderr, before the command's own body runs.
**`verify-run` is the sole exception**: it is dispatched before both of
these checks (`cli.py:560-564`), so it works even without `git` on `PATH`
`2` and `127` are listed in every per-command exit-code table below for
that command's own argument-validation failures. A `127` there always also
covers this global `git`-missing check. Where a table does not separately
list `2` for a config-load failure, that command still returns `2` on an
unparsable `--config` file via this same global check — it happens before
the command's own body runs, so the table's own `2` entry (if any) never
applies to that case.

Every command below can also be reached as `python -m dreamy <command> ...`
(the two invoke the same `dreamy.cli:main` entrypoint).

## Table of Contents

- [Pipeline](#pipeline)
  - [`run`](#run)
  - [`status`](#status)
  - [`diff`](#diff)
  - [`import`](#import)
  - [`archives`](#archives)
- [Scheduling](#scheduling)
  - [`install`](#install)
  - [`uninstall`](#uninstall)
- [Surfaces](#surfaces)
  - [`tui`](#tui)
  - [`web`](#web)
- [Analysis](#analysis)
  - [`cost`](#cost)
  - [`findings`](#findings)
  - [`dismiss`](#dismiss)
  - [`undismiss`](#undismiss)
  - [`efficacy`](#efficacy)
- [Export](#export)
  - [`bundle`](#bundle)
- [Maintenance](#maintenance)
  - [`skills`](#skills)
  - [`doctor`](#doctor)
  - [`verify-run`](#verify-run)
- [Validation surfaces (I6–I10)](#validation-surfaces-i6i10)
  - [Release-gate probes (I6)](#release-gate-probes-i6)
  - [Evidence-corpus binding (I7)](#evidence-corpus-binding-i7)
  - [Store close guard (I8)](#store-close-guard-i8)
  - [Connector cadence (I9)](#connector-cadence-i9)
  - [Doc-example harness (I10)](#doc-example-harness-i10)

---

## Pipeline

### `run`

**Synopsis**

```
dreamy run [-h] [--lookback-days LOOKBACK_DAYS]
           [--source-path SOURCE=PATH] [--agent-cap-usd AGENT_CAP_USD]
```

**What it does**

Runs the full reconciliation pipeline: ingests session records from all six
connectors, correlates them into per-project episodes, analyzes each project
(deterministic findings plus, if a positive spend cap is configured, agent-
assisted findings), reconciles compiler-stamped prompt artifacts against
today's compiler, and persists everything to `state.db` under `output_dir`.
This is the command that produces new session/finding data from source
material; `dismiss`/`undismiss` also write `state.db`, but only to toggle an
existing finding's dismissal state, not to create new findings. `run` is
what the launchd job scheduled by `install` invokes on a timer.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `--lookback-days LOOKBACK_DAYS` | int, no default (falls back to config `lookback_days`, default `30`) | How many days back to scan each source for records. Must be positive — `dreamy: --lookback-days must be positive` on `<= 0`. |
| `--source-path SOURCE=PATH` | repeatable string, default `[]` | Override the on-disk path a connector reads from, e.g. `--source-path claude=/custom/path`. Invalid syntax (`no '='`, empty source, or empty path) raises `dreamy: invalid --source-path '...'; expected SOURCE=PATH`. |
| `--agent-cap-usd AGENT_CAP_USD` | float, no default | Temporary positive per-run agent spend cap, overriding `config.json`'s `spend_cap_usd` for this run only. `<= 0` is rejected with `dreamy: --agent-cap-usd must be positive`. |

**Example**

```
$ dreamy run --lookback-days 7
{"agent_call_records": [], "agent_cost_usd": 0, "agent_findings_persisted": 0,
 "agent_run_status": "disabled", "ended_ms": 1785814470338, "error_count": 0,
 "finding_count": 13, "prompt_count": 10,
 "run_id": "20260803T233354-7eb1f70f",
 "skipped_projects": [],
 "source_counts": {"claude": 8089, "codex": 0, "nine_router": 62939,
                    "omp": 141109, "opencode": 816, "pi": 0},
 "started_ms": 1785814434506, "status": "ok", "warning_count": 7,
 "warnings": ["pi: no records within 7d window (path ~/.pi/agent/sessions, never ingested)",
              "codex: no records within 7d window (path ~/.codex, never ingested)",
              "... [output trimmed]"]}
```

The single line of JSON above is the entire stdout of a real run; it has been
reformatted for readability and its `warnings` array trimmed.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | `result.status == "ok"` — the run completed and committed. |
| `2` | Invalid arguments (`--lookback-days <= 0`, `--agent-cap-usd <= 0`, malformed `--source-path`, or an unparsable `--config` file). |
| `3` | `result.status != "ok"` — the pipeline returned a non-`"ok"` status. `status` can be `"lock_held"` (another run already holds the run lock) or `"failed"` (an unhandled exception during the run, caught by the outer `except Exception`). |
| `127` | The `git` binary was not found on `PATH`. `dreamy` requires `git` for deterministic analysis and refuses to start any command (except `verify-run`) without it. |

**Notes / gotchas**

- `run` is exclusive: a `RunLock` prevents two runs from executing
  concurrently against the same `output_dir`. A second `run` invoked while
  one is already in progress exits `3` with `status: "lock_held"` rather than
  corrupting state.
- The `git` binary check (exit `127`) happens before config is even loaded,
  so it applies to every subcommand except `verify-run`, which is dispatched
  before the check.

[↑ Back to top](#table-of-contents)

---

### `status`

**Synopsis**

```
dreamy status [-h]
```

**What it does**

Emits a single line of machine-readable JSON describing the latest run (from
`state.db`) and the current launchd schedule state (from live `launchctl`
output). This is the fastest way to check "is dreamy scheduled, and did it
last succeed" without opening the TUI or web dashboard.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |

**Example**

```
$ dreamy status
{"latest_run": {"agent_cost_usd": 0.0, "duration_ms": null, "ended_ms": null,
  "finding_count": 0, "id": "20260803T231804-4b728d0a", "lookback_days": 30,
  "prompt_count": 0, "source_counts": {}, "started_ms": 1785813485345,
  "status": "running"},
 "schedule": {"installed": true, "interval_seconds": 21600,
  "label": "com.nick.dreamy", "last_exit": "(never exited)",
  "next_run_ms": 1785835085345,
  "plist_path": "~/Library/LaunchAgents/com.nick.dreamy.plist",
  "raw": "gui/501/com.nick.dreamy = { ... }"}}
```

(`raw` and the deeper `launchctl print` fields have been elided above for
readability; the real output is unformatted single-line JSON.)

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Always — `status` has no failure branch of its own. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH` (checked before every subcommand). |

**Notes / gotchas**

- `latest_run` is `null` if no run has ever committed a `runs` row.
- `schedule` is `null` if `install` has never been run — `status` never
  fails just because the job isn't scheduled yet.
- Per-source **cadence** (I9) is not part of this JSON. Cadence lives on
  `ReadStore.source_stats()` and is exposed on the web overview payload
  (`GET /api/v1/overview` → `source_stats[].cadence`). See
  [Connector cadence (I9)](#connector-cadence-i9).

[↑ Back to top](#table-of-contents)

---

### `diff`

**Synopsis**

```
dreamy diff [-h] [--json]
```

**What it does**

Compares the findings from the two most recent runs and reports which
findings are `new`, `resolved`, `persisting`, or `regressed` since the prior
run. It always diffs against the latest run in `state.db` (there is no way
to select an arbitrary pair of runs from the CLI).

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `--json` | flag, default off | Emit machine-readable JSON instead of the human-readable text table. |

**Example**

```
$ dreamy diff
Diff — to run 20260803T231804-4b728d0a
  (no findings)
```

JSON form:

```
$ dreamy diff --json
{
  "counts": {},
  "findings": [],
  "from_run": null,
  "to_run": "20260803T231804-4b728d0a"
}
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Success (including the "no findings" / "no runs yet" case is `2`, see below). |
| `2` | `state.close()`d before any diff: no runs at all exist in `state.db` — `dreamy: no runs in state DB`. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- `from_run` in the JSON output is always `null` from the CLI — the diff is
  always relative to whatever the store's internal `findings_diff` resolves
  as the prior run, not a caller-selected run id.

[↑ Back to top](#table-of-contents)

---

### `import`

**Synopsis**

```
dreamy import [-h] [--json] path
```

**What it does**

Reads the machine-readable payload embedded in a `dreamy bundle` HTML file and
records it as an **archival snapshot** in the `imported_*` tables.

This is deliberately **not** a project restore. The exported payload carries
per-bundle opaque ids and timestamps only — no project link, no source id, no
role, model, content, or canonical id — so an import can reconstruct a
timeline and volumes, but cannot attribute a turn to a project, recompute
canonical ids, correlate across harnesses, or feed analysis. Imported rows
share no key space with locally-ingested `sessions`/`turns`, so an import can
never collide with, overwrite, or resurrect local data.

Bundles are untrusted input. The whole file is validated before a single row
is written — schema version, id shape, integer ranges, unknown keys, duplicate
ids, row caps, and a 256 MB file-size limit — and any violation rejects the
entire file rather than importing part of it.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `path` | positional, required | Bundle `.html` produced by `dreamy bundle`. |
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `--json` | flag, default off | Emit machine-readable JSON instead of the text summary. |

**Example**

```
$ dreamy --config ~/.config/dreamy/config.json import ~/bundles/myproject.html
imported: bundle 0f3a91c6d4b27e58 (myproject) — 1234 session(s), 5678 turn(s)
archival snapshot only: timelines and volumes, not a project restore
```

Re-importing the same file is idempotent:

```
$ dreamy import ~/bundles/myproject.html --json
{"already_imported": true, "bundle_id": "0f3a91c6d4b27e58a1b2c3d4e5f60718",
 "exported_ms": 1785817088282, "project_name": "myproject",
 "sessions_written": 1234, "turns_written": 5678, "warnings": []}
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Imported, or an identical bundle was already present (idempotent). |
| `2` | Bundle rejected: malformed, oversized, unsupported `schema_version`, duplicate JSON keys, more than one payload element, or a re-import whose `bundle_id` matches an existing snapshot with **different** content. |
| `2` | *(also)* `--config` names a file that fails to parse — checked globally before this command's body runs. |
| `127` | `git` binary missing on `PATH`. |

**Sensitivity**

A bundle carries the same project narrative the HTML already renders on
screen: project name and path, git remote, intent episodes, findings, and the
four compiled prompt bodies. Session and turn rows are reduced to opaque ids
and timestamps, but the narrative is real content — treat a bundle as
sensitive as the project it describes.

**Notes / gotchas**

- Idempotency keys on **content**, not on the id alone. `imported_bundles`
  stores a canonical SHA-256 of the validated payload; a re-import whose
  `bundle_id` matches but whose digest differs is refused, so an id collision
  or a tampered re-send cannot be silently accepted as "already imported".
- Bundle ids are random per export, so two exports of the same project import
  as two independent snapshots. That is the deliberate cost of making bundles
  unlinkable — the HMAC key used to derive export ids is discarded at export
  time and never ships.
- `--config`'s `output_dir` selects the target store. `DREAMY_OUTPUT_DIR` is
  **not** honoured by this command.

[↑ Back to top](#table-of-contents)

---

### `archives`

**Synopsis**

```
dreamy archives [-h] [--json]
```

**What it does**

Lists imported bundle snapshots, newest first. Read-only: it never creates or
migrates a store, and it deliberately performs no join to `projects`,
`sessions`, or `turns` — an imported bundle shares no key space with local
data, and a join that appeared to work would imply an equivalence that does
not exist.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `--json` | flag, default off | Emit machine-readable JSON instead of the text table. |

**Example**

```
$ dreamy archives
Imported snapshots (1)
  bundle             project                   sessions     turns  source
  0f3a91c6d4b27e58   myproject                    1,234     5,678  myproject.html
```

On a machine with no snapshots (and no state DB yet):

```
$ dreamy archives
no imported bundles

$ dreamy archives --json
[]
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Success, including the empty case and the no-state-DB case. |
| `2` | `--config` names a file that fails to parse — checked globally before this command's body runs. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- A missing `state.db` is reported as "no imported bundles", not as an error:
  the read surface is read-only by design and cannot create a store.

[↑ Back to top](#table-of-contents)

---

## Scheduling

### `install`

**Synopsis**

```
dreamy install [-h]
```

**What it does**

Writes (or refreshes) a `com.nick.dreamy` `launchd` plist under
`~/Library/LaunchAgents/`, pointing `ProgramArguments` at the *currently
running* Python interpreter (`sys.executable`) invoking `-m dreamy run`, then
runs `launchctl bootstrap` to load it. The job fires every
`interval_seconds` (from config, default `21600` = 6 hours) and its stdout /
stderr are redirected to `~/.local/share/dreamy/logs/launchd.{out,err}.log`.

**This is destructive to the previous schedule state**: it overwrites any
existing plist for the same label and reloads the job.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |

Behavior (which interval, which interpreter, which output directory) is
entirely config-driven, not flag-driven.

**Example — refused (interpreter lives inside the source checkout)**

```
$ cd /path/to/dreamer && .venv/bin/python -m dreamy install
refusing to install a scheduled job pinned to a non-durable interpreter: /path/to/dreamer/.venv/bin/python lives inside the source checkout /path/to/dreamer; moving, renaming, or deleting that checkout breaks every future run.
The plist outlives this shell, so launchd would fail to spawn with no visible error the next time the job fires.
Install dreamy under a persistent path first, e.g.:
  python3 -m venv ~/.venvs/dreamy
  ~/.venvs/dreamy/bin/pip install <path-to-checkout>
  ~/.venvs/dreamy/bin/dreamy install
(a non-editable install — `-e` leaves the package in the checkout)
```
Exit code: `2`.

**Example — succeeds (interpreter is a durable, non-editable install)**

```
$ ~/.venvs/dreamy/bin/dreamy install
{"plist": "~/Library/LaunchAgents/com.nick.dreamy.plist", "launchctl": "", "installed": true}
```
Exit code: `0`.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Plist written, `launchctl bootstrap` succeeded, and `launchctl print` confirms the job is loaded (`is_installed()` returns `True`). |
| `2` | `UndurableInterpreterError` — `sys.executable` lives inside a source checkout or another ephemeral/temp root, so the interpreter would not survive a checkout deletion or a reboot-cleared temp directory. The error message includes the exact remediation (create a durable venv, install non-editable, install from there). |
| `4` | The plist was written and `launchctl bootstrap` was attempted, but `is_installed()` still returns `False` afterward — the bootstrap did not actually take. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- **Durable install is mandatory for scheduling.** `dreamy install` refuses
  any interpreter path inside the git checkout, or inside a recognized
  ephemeral/temp root, with exit `2`. Editable installs (`pip install -e`)
  from the checkout are explicitly unsupported for this reason — the
  interpreter path resolves back into the checkout either way.
- The correct pattern is: `python3 -m venv ~/.venvs/dreamy && ~/.venvs/dreamy/bin/pip install <checkout-path>` (non-editable) `&& ~/.venvs/dreamy/bin/dreamy install`.
- Re-running `install` while the job is already loaded refreshes the plist
  and reloads it — this is the supported way to pick up a changed
  `interval_seconds`.

[↑ Back to top](#table-of-contents)

---

### `uninstall`

**Synopsis**

```
dreamy uninstall [-h]
```

**What it does**

Runs `launchctl bootout` against the `com.nick.dreamy` label if it is
currently loaded, removing the scheduled job. **This stops all automatic
reconciliation** — no future `run` invocations will happen until `install`
is run again.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |

**Example**

```
$ dreamy uninstall
{"launchctl": "", "installed": true}
```
Exit code: `4` in the example above — the job was still reported as loaded
immediately after the bootout attempt (see Exit codes below for why this can
happen transiently).

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | `launchctl bootout` ran and `launchctl print` subsequently confirms the job is no longer loaded (`is_installed()` returns `False`). |
| `4` | `launchctl bootout` ran, but `is_installed()` still reports `True` immediately afterward — the unload did not fully take effect (this can be a `launchctl` state-propagation race; re-run `status` a moment later to confirm). |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- **This is a destructive/irreversible-until-reinstalled action.** After
  `uninstall`, `dreamy run` will not fire on any schedule; session data will
  stop being reconciled until `install` is run again.
- Uninstalling does not delete the plist file or any state in `state.db` —
  only the loaded launchd registration.

[↑ Back to top](#table-of-contents)

---

## Surfaces

### `tui`

**Synopsis**

```
dreamy tui [-h]
```

**What it does**

Launches a full-screen Textual dashboard (`DreamyApp`) over the same
`state.db` used by `run`. Seven views — Runs, Findings, Projects, Prompts,
Schedule, Monitor, and a "next-run projects" filter view — are reachable via
number keys `1`–`7`. A permanent `⬤ REPO: READ-ONLY` indicator is always
visible. This is a blocking, interactive command that occupies the terminal
until you quit (`q`).

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |

**Example**

```
$ dreamy tui --help
usage: dreamy tui [-h]

options:
  -h, --help  show this help message and exit
```

`dreamy tui` itself launches a blocking, full-screen interactive dashboard —
captured below from a real run against the live `state.db` (a PTY render,
trimmed to the top-left panels and the footer status bar; box-drawing
characters may not align perfectly outside a real terminal):

```
$ dreamy tui
 1 Runs  2 Findings  3 Projects  4 Prompts  5 Schedule  6 Monitor  7 Next-run projects  ? Help  / Search

 ┌──────────────────────────────────────────────────────────────┐
 │ LAST RUN                                                      │
 │                                                                │
 │ 2026-08-04 03:38  ● ok   duration 13.3s                        │
 │ projects 48   findings 0   prompts 0                            │
 │ agent spend $0.0000                                             │
 └──────────────────────────────────────────────────────────────┘

 ┌───────────────────────────────┐ ┌────────────────────────────┐
 │ SOURCES                        │ │ ATTENTION                   │
 │   ○ claude   not installed     │ │   ▲ 37 high-severity finding(s) open │
 │   ● nine_router  17,699 records│ │   ◇ 22 completion(s) unverified │
 │   ● omp          40,359 records│ │   ○ 4 source(s) contributed nothing │
 └───────────────────────────────┘ └────────────────────────────┘

 started           status         duration  findings  prompts  cost
 2026-08-04 03:38  ● ok           13.3s     0         0        $0.0000
 2026-08-04 03:37  ● ok           18.1s     0         0        $0.0000
 ... [output trimmed]

 agent spend: $10.8948   next run: 09:38 UTC   ⬤ REPO: READ-ONLY
```

Key bindings (from `DreamyApp.BINDINGS`):

| Key | Action |
|---|---|
| `1`–`7` | Switch tab: Runs, Findings, Projects, Prompts, Schedule, Monitor, Next-run projects |
| `?` | Help screen |
| `/` | Search |
| `q` | Quit |
| `r` | Run now |
| `f` | Filter |
| `v` | Finding state filter |
| `y` | Yank |
| `o` | Open (editor) |
| `h` | Harness |
| `d` | Dismiss finding *(destructive; routes through a confirmation modal)* |
| `x` | Undismiss finding *(destructive; routes through a confirmation modal)* |
| `Ctrl+S` | Save project filters |
| `a` | Abort run *(destructive; routes through a confirmation modal)* |
| `i` | Install schedule *(destructive; routes through a confirmation modal)* |
| `u` | Uninstall schedule *(destructive; routes through a confirmation modal)* |
| `Escape` | Back |

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Always returned after the app exits normally (e.g. `q`). |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH` (checked before launch). |

**Notes / gotchas**

- Unlike `web`, `tui` shares the same `ReadStore` that has write access for
  the R20 dismiss/undismiss path — dismiss/undismiss/install/uninstall/abort
  actions triggered from the TUI are real, and each is gated behind an
  explicit confirmation modal (`x` to confirm, `Escape`/`n` to cancel).
- No SQL or `sqlite3` import is permitted anywhere under `tui/` per
  ADR-006 — all data access goes through `ReadStore`.

[↑ Back to top](#table-of-contents)

---

### `web`

**Synopsis**

```
dreamy web [-h] [--host HOST] [--port PORT]
```

**What it does**

Serves a read-only HTTP dashboard over `state.db`: a static single-page
frontend plus a JSON API under `/api/v1/*`. The dashboard renders the same
underlying data as the TUI (runs, findings, projects, prompts, schedule,
monitor events) but has no write endpoints — every request goes through a
`ReadStore` opened with `read_only=True`, so the process physically cannot
mutate `state.db`. This command blocks the terminal, serving forever until
interrupted with Ctrl-C.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `--host HOST` | string, default `127.0.0.1` | Bind address; **must resolve to loopback only**. A non-loopback host is rejected before the socket is opened. |
| `--port PORT` | int, default `8765` | Bind port. |

**Example**

```
$ dreamy web --port 8877
dreamy web: http://127.0.0.1:8877  (read-only; ctrl-c to stop)
```

Real HTTP responses from a running server:

```
$ curl -s http://127.0.0.1:8877/healthz
{"status":"ok","has_state":true}
```

Confirmed routes and their real HTTP status codes observed against a live
server (from the acceptance evidence run):

```
200  /healthz
200  /api/v1/overview
200  /api/v1/runs
200  /api/v1/findings
200  /api/v1/projects
200  /api/v1/prompts
200  /api/v1/schedule
200  /api/v1/monitor
404  /api/anything-else   (any unrecognized route)
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Server ran and was cleanly stopped with Ctrl-C (`KeyboardInterrupt`), or ran and returned normally. |
| `2` | `build_server` rejected `--host` because it does not resolve to loopback — `dreamy: <message>`. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- **Loopback-only by design (R17a).** A non-loopback `--host` is rejected
  before the socket is ever opened, so an operator cannot accidentally
  expose their full session history to the network with a mistyped flag.
- No SQL or `sqlite3` import is permitted anywhere under the web surface
  per ADR-006 (verified by `dreamy doctor`'s `ADR006-1` check).
- The dashboard is served over plain HTTP with no authentication — this is
  acceptable only because it is loopback-bound; do not put anything in
  front of it that forwards non-loopback traffic.

[↑ Back to top](#table-of-contents)

---

## Analysis

### `cost`

**Synopsis**

```
dreamy cost [-h] [--json] [--project PROJECT] [--window {daily,monthly}]
```

**What it does**

R10 cost rollup. Without `--project`, prints the full 30-day cost breakdown
across every project plus an "unattributed" bucket for router requests that
carry no defensible session link (most router requests fall in this
bucket — it is shown deliberately rather than hidden, since hiding it would
silently understate total spend). With `--project`, scopes the report to a
single project's 30-day cost.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `--json` | flag, default off | Emit machine-readable JSON instead of the text table. |
| `--project PROJECT` | string, default `None` | Project id; scopes the report to that project's 30-day cost. |
| `--window {daily,monthly}` | choice, default unset | Report the cumulative spend window the agent-layer cap is enforced against, rather than the per-project rollup. `daily` is the trailing 24h, `monthly` the trailing 30d. Mutually meaningful with neither `--project` nor the default view: it reads the `spend_ledger`, not per-project attribution. |

**Example — full rollup**

```
$ dreamy cost
Cost rollup
  attributed    $  123.4567
  unattributed  $  456.7890  (12345 requests with no defensible session link)
  confidence    {'high': 987}

  project                              total_usd    requests
  /                                 $     1.0000           4
  myproject                         $    42.1234         567
  another-project                   $    80.3333        1024
  ... [output trimmed; one row per attributed project]
```

**Example — scoped to one project**

```
$ dreamy cost --project 71074d298e5bcc42
30-day cost — project 71074d298e5bcc42
  total                $    4.4894
  episode_count                  5
  per_completed_intent $    0.0000
  confidence_mix       {'high': 5}
```

**Example — unknown project**

```
$ dreamy cost --project this-project-does-not-exist
dreamy: unknown project id: this-project-does-not-exist
```
Exit code: `2`.

**Example — cumulative window (what the cap is enforced against)**

```
$ dreamy cost --window daily
Cumulative cost — daily (trailing 24h)
  spent      $    0.0000
  cap        (not configured — spend_cap_daily_usd is null)

$ dreamy cost --window monthly
Cumulative cost — monthly (trailing 30d)
  spent      $    0.0000
  cap        (not configured — spend_cap_monthly_usd is null)
```

With caps configured and spend on the ledger (seeded store — three runs at
$4.00 ten days ago, $2.50 six hours ago, $1.25 two hours ago):

```
$ dreamy cost --window daily
Cumulative cost — daily (trailing 24h)
  spent      $    3.7500
  cap        $   10.0000
  remaining  $    6.2500

$ dreamy cost --window monthly
Cumulative cost — monthly (trailing 30d)
  spent      $    7.7500
  cap        $   50.0000
  remaining  $   42.2500
```

The windows are **trailing, not calendar**: the $4.00 run ten days ago is
outside the 24h window and inside the 30d one, which is why daily reports
$3.75 and monthly $7.75.

This is a different question from the rollup above. The rollup answers "what
did each project cost"; `--window` answers "how much agent-layer headroom is
left in the window".

What the cap actually gates: `dreamy run` still runs, ingesting, correlating,
and analyzing deterministically as usual. Only the **paid agent layer** is
skipped, and each affected project is reported in `skipped_projects` with the
reason `agent_skipped_cumulative_cap_daily` or `..._monthly`. The run is not
refused and no finding state is transitioned on account of the skip.

Admission is checked once, pre-flight, before any project issues a paid call:
a run's agent layer is admitted only when `spent + per_run_cap <= window_cap`.
Equality admits. This is deliberately stricter than "stop once spent exceeds
the cap", which would let a run start with $9 against a $10 daily cap and then
spend its full $5 per-run budget for a $14 total.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Success, both for the full rollup and the `--project` form (including a project with zero router requests attributed, which is reported as a legitimate zeroed result, not an error). |
| `2` | `--project` names a project id that `state.project_detail()` does not recognize. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- The `unattributed` bucket is intentionally surfaced alongside the
  attributed total in the non-`--project` view — most router requests carry
  no defensible session link, and hiding that would understate total spend.

[↑ Back to top](#table-of-contents)

---

### `findings`

**Synopsis**

```
dreamy findings [-h] [--state STATE] [--severity SEVERITY]
                 [--category CATEGORY] [--project PROJECT] [--json]
                 [--limit LIMIT] [--metrics]
```

**What it does**

R13 findings listing. Reads the `findings` table filtered by delta state
(`new`, `persisting`, `resolved`, `regressed`, `dismissed`), optional
severity/category/project filters, with an optional row cap. **The default
delta-state filter, with no `--state` at all, is `new,regressed`** — not
"all findings".

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `--state STATE` | repeatable string, default `None` (resolves to `new,regressed`) | Filter by delta state: `new`, `persisting`, `resolved`, `regressed`, `dismissed`. Repeatable to pass several. Pass the single token `all` to disable state filtering entirely. Mixing `all` with other values, or passing an unrecognized state, is rejected. |
| `--severity SEVERITY` | string, default `None` | Filter by severity. |
| `--category CATEGORY` | string, default `None` | Filter by category. |
| `--project PROJECT` | string, default `None` | Filter by project id. |
| `--json` | flag, default off | Emit machine-readable JSON instead of the text table. |
| `--limit LIMIT` | int, default `None` | Cap the number of rows returned. Must be positive. |
| `--metrics` | flag, default off | Replace the listing with the R22 lifecycle rollup: per-category and per-severity time-to-resolution, regression rate, backlog delta, open count, and age percentiles. Ignores the state/severity/category filters — it reports over the whole ledger, not a slice. |

**Example**

```
$ dreamy findings --limit 3
Findings (3 rows)
  id                state       prov    sev       category        title
  5954cdc9d6a7830c  new         (D)     low       incomplete_work  Project directory no longer exists
  68ec43f9e36c95f8  new         (D)     low       incomplete_work  Project directory no longer exists
  f68177acf29d2458  new         (D)     low       incomplete_work  Project directory no longer exists
```

JSON form:

```
$ dreamy findings --json --limit 2
[
  {
    "category": "incomplete_work",
    "delta_state": "new",
    "dismissal_reason": null,
    "id": "5954cdc9d6a7830c",
    "project_id": "4fe8f2293e64c44b",
    "provenance": "(D)",
    "severity": "low",
    "title": "Project directory no longer exists"
  },
  {
    "category": "incomplete_work",
    "delta_state": "new",
    "dismissal_reason": null,
    "id": "68ec43f9e36c95f8",
    "project_id": "c4b4097ac8cb9e45",
    "provenance": "(D)",
    "severity": "low",
    "title": "Project directory no longer exists"
  }
]
```

**Example — `--metrics` lifecycle rollup (R22)**

Output below is from a seeded three-run store, not a live one: a doc example
that ships real counts and ages discloses how much work an operator has
outstanding and how long it has sat.

```
$ dreamy findings --metrics
Finding metrics — Category (R22)
  category                      ttr_ms  regr_rate  backlog_Δ   open    age_p50_ms    age_p90_ms  not_obs
  docs_drift                  86400000     0.0000         -1      0                                    0
  incomplete_work                                          1      1     172800000     172800000        0
  tech_debt                   86400000     1.0000          1      1     259200000     259200000        0

Finding metrics — Severity (R22)
  severity                      ttr_ms  regr_rate  backlog_Δ   open    age_p50_ms    age_p90_ms  not_obs
  high                                                     1      1     172800000     172800000        0
  low                         86400000     0.0000         -1      0                                    0
  medium                      86400000     1.0000          1      1     259200000     259200000        0
```

Three findings produced those rows, one per lifecycle shape:

- `docs_drift` was seen, then resolved, and never came back — `regr_rate`
  `0.0000`, `backlog_Δ` `-1`, `open` `0`, and no age (nothing is still open).
- `tech_debt` resolved and then regressed — `regr_rate` `1.0000`, and it is
  open again.
- `incomplete_work` has never resolved — `ttr_ms` is **blank**, not `0`.

The ages are exact by construction: `86400000` ms is one day, `172800000`
two, `259200000` three, matching when each finding was seeded. A live capture
carries a few ms of execution time in the last digits; these are rounded to
the seeded intervals so the example teaches the unit rather than a timing
artifact. A blank `ttr_ms` means nothing in that bucket has ever resolved,
which is a different statement from `0`.

Reading the columns:

- `ttr_ms` — median time from first observation to resolution. Blank means
  nothing in that bucket has ever resolved, which is not the same as zero.
- `regr_rate` — share of resolved findings that later came back. `1.0000`
  means everything that resolved regressed.
- `backlog_Δ` — net change in open count across the observed runs.
- `not_obs` — findings whose owning project was not in the analyzed set for
  any run. They are excluded from every rate above rather than counted as
  either open or resolved, because absence of observation is not evidence.

**Example — invalid `--state`**

```
$ dreamy findings --state bogus
dreamy: unknown --state value(s): bogus (expected one of: dismissed, new, persisting, regressed, resolved, or 'all')
```
Exit code: `2`.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Success. |
| `2` | `--limit <= 0`, an unrecognized `--state` value, or `--state all` combined with any other `--state` token. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- `provenance` is `(D)` for deterministic (rule-based) findings and `(A)`
  for agent-derived findings.
- The default filter (`new,regressed`) is a deliberate design choice per the
  R13 spec — running `findings` with no flags shows only what's actionable,
  not the full historical set. Pass `--state all` to see everything
  including `dismissed` and `resolved`.

[↑ Back to top](#table-of-contents)

---

### `dismiss`

**Synopsis**

```
dreamy dismiss [-h] --reason {wrong,wont-fix,already-done,duplicate} finding_id
```

**What it does**

R20: dismiss a finding, marking it resolved with an attached reason code.
**This is reversible** via `undismiss`. The finding's current evidence
digest is pinned at dismissal time, so a later run can distinguish "same
finding, already judged" from "materially changed recurrence."

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `finding_id` | positional, required | The id of the finding to dismiss. |
| `--reason` | required, one of `wrong`, `wont-fix`, `already-done`, `duplicate` | The dismissal reason code. This is the closed vocabulary the `findings` command's dismissal-rate metric depends on — no free-text reasons are accepted. |

**Example — success**

```
$ dreamy dismiss a9c4cf265d7a972c --reason wont-fix
{"finding_id": "a9c4cf265d7a972c", "dismissed": true, "reason": "wont-fix"}
```

**Example — unknown finding id**

```
$ dreamy dismiss nonexistent-id --reason wrong
dreamy: unknown finding id: nonexistent-id
```
Exit code: `2`.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | The finding existed and was dismissed. |
| `2` | `finding_id` does not exist in `state.db`. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- **Reversible, not destructive** — a dismissal can always be undone with
  `undismiss` for the same `finding_id`.
- `--reason` is validated against a closed set at the argparse level
  (`{wrong,wont-fix,already-done,duplicate}`); anything else is rejected by
  argparse before the command body even runs.

[↑ Back to top](#table-of-contents)

---

### `undismiss`

**Synopsis**

```
dreamy undismiss [-h] finding_id
```

**What it does**

R20: reverses a previous `dismiss` on `finding_id` — clears
`resolved_ms`, `dismissal_reason`, `dismissed_evidence_sha`, and
`dismissed_ms` for that finding, returning it to its prior (non-dismissed)
state.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `finding_id` | positional, required | The id of the finding to undismiss. |

**Example — success**

```
$ dreamy undismiss a9c4cf265d7a972c
{"finding_id": "a9c4cf265d7a972c", "undismissed": true}
```

**Example — unknown finding id**

```
$ dreamy undismiss nonexistent-id
dreamy: unknown finding id: nonexistent-id
```
Exit code: `2`.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | A row matching `finding_id` was updated (`cur.rowcount` was truthy). |
| `2` | No row matched `finding_id` — nothing to undismiss. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- `undismiss` succeeds silently on a finding that was never dismissed in the
  first place as long as the `finding_id` exists — the SQL `UPDATE` simply
  no-ops the already-`NULL` columns, and `rowcount` still counts the
  matched row. It only returns `2` when the id itself doesn't exist.
- This command writes directly to `state.db`; run it from the same
  environment that has write access (i.e. not through the loopback web
  dashboard, which is strictly read-only).

[↑ Back to top](#table-of-contents)

---

### `efficacy`

**Synopsis**

```
dreamy efficacy [-h] [--json]
```

**What it does**

R21: prompt efficacy report. Measures, per artifact type
(`next_tasks`, `remediation`, `resumption`, `validation`), whether a
compiled prompt artifact was ever observed being acted on in a later
transcript, and if so, whether it was `completed_after_use` or
`unresolved_after_use`. **`not_observed` is explicitly never treated as a
failure** — it means there is no evidence either way, not that the artifact
failed.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `--json` | flag, default off | Emit machine-readable JSON instead of the text table. |

**Example**

```
$ dreamy efficacy
Efficacy (R21)

  artifact_type    total  observed  not_observed     rate
  next_tasks          32         0            32   0.0000
    harnesses: {}
    completed_after_use: 0  unresolved_after_use: 0
  remediation         31         0            31   0.0000
    harnesses: {}
    completed_after_use: 0  unresolved_after_use: 0
  resumption          32         0            32   0.0000
    harnesses: {}
    completed_after_use: 0  unresolved_after_use: 0
  validation          29         0            29   0.0000
    harnesses: {}
    completed_after_use: 0  unresolved_after_use: 0

note: not_observed means the artifact was never seen in a later transcript — no evidence either way. It is never counted as a failure.
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Always — `efficacy` has no failure branch of its own. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- Uses the raw `Store` (not `ReadStore`) internally, since the report needs
  lower-level access than the read-only wrapper exposes.
- A `0.0000` `rate` with all-zero `observed`/`completed_after_use` (as in
  the example above) is a legitimate outcome on a store with little
  transcript history — it is not an error state.

[↑ Back to top](#table-of-contents)

---

## Export

### `bundle`

**Synopsis**

```
dreamy bundle [-h] [--output OUTPUT] project_id
```

**What it does**

R14: exports a single project's data as one self-contained HTML file — no
external assets, no network calls at view time — suitable for sharing
outside the machine dreamy runs on. `dreamy doctor`'s `R14-1` check verifies
every exported bundle contains zero secrets, zero home paths, and zero
external references.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `project_id` | positional, required | The project id to bundle. |
| `--output OUTPUT` | string, default `<output_dir>/bundles/<project_id>.html` | Output path for the generated HTML file. |

**Example**

```
$ dreamy bundle 71074d298e5bcc42 --output /tmp/vg3-bundle.html
/private/tmp/vg3-bundle.html
```

The command prints the resolved, absolute path of the file it wrote.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Bundle exported successfully; the written path is printed. |
| `2` | `export_project_bundle` raised `ValueError` — most commonly an unknown `project_id`. The message is printed as `dreamy: <message>`. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- Every bundle is a single HTML file — nothing is written outside the one
  target path (or its parent directories, which are not auto-created if
  they don't already exist under a custom `--output`).
- Bundle hygiene (no secrets, no home paths, no external assets) is
  continuously checked by `dreamy doctor`'s `R14-1` check against the most
  recently exported bundle.

[↑ Back to top](#table-of-contents)

---

## Maintenance

### `skills`

**Synopsis**

```
dreamy skills [-h] [--lock LOCK] {status,refresh,verify}
```

**What it does**

Inspects, refreshes, or verifies the content-addressed pins for every skill
the prompt compiler may inline into a compiled artifact. `status` reports
each skill's current on-disk hash against its pinned hash in `state.db`;
`refresh` re-pins any drifted skill to its current on-disk content;
`verify` checks the packaged (or a supplied) lock manifest against the
filesystem directly — the only one of the three that needs no `state.db`,
which makes it the bootstrap check for a machine that has never run
`dreamy run`.

Skills are pinned deliberately: two unchanged runs stay byte-identical only
because an inlined skill body is a fixed snapshot rather than "whatever the
file on disk currently says." Without an explicit `refresh`, an improved
skill body can never reach future compiled prompts — this is a one-way trap
by design (Q11).

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `skills_action` | positional, required, one of `status`, `refresh`, `verify` | Which operation to perform. |
| `--lock LOCK` | string, default `None` (the packaged lock) | Skill lock manifest path, used only by `verify`. |

**Example — `status`**

```
$ dreamy skills status
{
  "action": "status",
  "skills": [
    {
      "current_sha256": "924d786b6146caf1",
      "drifted": true,
      "pinned_sha256": null,
      "skill": "task-architect",
      "status": "drifted"
    },
    ... [output trimmed — 10 skills total]
  ]
}
```

**Example — `verify`**

```
$ dreamy skills verify
{
  "checked": 10,
  "failed": 0,
  "ok": true,
  "skills": [
    {
      "actual_sha256": "b5e4e2eb1939e20d80ebb08af2cd03e925ed48737ecb4b131cc668382a1e7fb5",
      "expected_sha256": "b5e4e2eb1939e20d80ebb08af2cd03e925ed48737ecb4b131cc668382a1e7fb5",
      "installable": false,
      "observed_absolute_path": "~/.claude/plugins/cache/agentkit-local/ak-engineer/0.2.0/skills/ak-research-prompt/SKILL.md",
      "path": "~/.claude/plugins/cache/agentkit-local/ak-engineer/0.2.0/skills/ak-research-prompt/SKILL.md",
      "relocated": false,
      "remediation": null,
      "required": false,
      "skill": "ak-research-prompt",
      "status": "ok"
    },
    ... [output trimmed — 10 skills total]
  ]
}
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | `status`/`refresh`: always (no failure branch). `verify`: every checked skill matched its pinned hash (`report.ok == True`). |
| `2` | `verify` only — the lock manifest itself could not be loaded (`LockError`), e.g. missing or malformed lock file. |
| `5` | `verify` only — at least one *required* skill failed verification (missing on disk, relocated, or hash mismatch). Each failing skill's remediation is printed to stderr. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. `skills` (all three actions, including `verify`) is dispatched through the standard `main()` flow, which checks `git` before any command body runs — only `verify-run` is exempt from this check. |

**Notes / gotchas**

- `verify` is the only `skills` action that works on a machine that has
  never run `dreamy run` — it checks the lock against the filesystem only,
  with no dependency on `state.db`.
- `status` compares against the pin recorded in `state.db` (requires a prior
  run); `verify` compares against a static lock manifest (does not).
  They answer different questions and are not interchangeable.

[↑ Back to top](#table-of-contents)

---

### `doctor`

**Synopsis**

```
dreamy doctor [-h] [--json]
```

**What it does**

I1: runs the currently *implemented* subset of acceptance diagnostics
against the real system — state DB integrity, no-SQL-in-surfaces (ADR-006),
connector conformance (R1), git CLI presence (R4), read-only enforcement
(R9), zero-secrets-persisted (N4), bundle hygiene (R14), absolute evidence
paths (R6g), no unresolved skill refs (R6f), and the TUI's permanent
read-only indicator (C.2). **This is explicitly PARTIAL** — it is not the
full acceptance matrix (see `docs/acceptance/GATE-MATRIX.md`); it is only
the checks that have actually been implemented so far.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `--json` | flag, default off | Emit machine-readable JSON instead of the human-readable text report. |

**Example**

```
$ dreamy doctor
dreamy doctor — implemented diagnostics (partial; not the full matrix)

  [PASS] STORE-1   R2 state integrity
         integrity_check=ok schema_version=7
  [PASS] ADR006-1  ADR-006 no SQL in surfaces
         0 offending file(s)
  [PASS] R1-1      R1 connector conformance
         6 connectors: claude, codex, nine_router, omp, opencode, pi
  [PASS] R4-1      R4 git CLI present
         git version 2.55.0
  [PASS] R9-1      R9 read-only enforcement
         write refused by SQLite: attempt to write a readonly database
  [PASS] N4-1      N4 zero secrets persisted
         428 artifacts + all state.db columns scanned; 0 file hit(s), 0 row hit(s)
  [PASS] R14-1     R14 bundle hygiene
         182804 bytes, 0 secrets, 0 home paths, 0 external refs
  [PASS] R6g-1     R6g absolute evidence paths
         1 artifact(s), 0 relative dreamy-emitted evidence ref(s)
  [PASS] R6f-1     R6f no unresolved skill refs
         1 artifact(s), 0 unresolved ref(s)
  [PASS] C2-1      C.2 read-only indicator visible
         visible in 18/18 view x size frames

10 passed, 0 failed, 0 skipped in 13.7s
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Every implemented check ran and PASSed. |
| `1` | At least one check FAILed — a real, confirmed defect. |
| `2` | Nothing FAILed, but at least one check SKIPped — the implemented check set could not fully execute (common on a fresh machine with no state DB yet), so no verdict of "0" can be truthfully claimed. |
| `2` | *(also)* `--config` names a file that fails to parse (`ValueError` from `load_config()`) — checked globally before this command's body runs; see the Dispatch order note above. |
| `127` | `git` binary missing on `PATH`. |

**Notes / gotchas**

- **The three-way exit code split is deliberate.** Collapsing SKIP into PASS
  is exactly how an acceptance harness can report green while covering
  nothing — a check that did not run has proven nothing. Exit `2` is the
  honest signal for "incomplete, not proven either way."
- `doctor`'s promise is scoped strictly to the checks it implements; it
  never claims to certify the full `GATE-MATRIX.md` acceptance surface.

[↑ Back to top](#table-of-contents)

---

### `verify-run`

**Synopsis**

```
dreamy verify-run [-h] [--repo-root REPO_ROOT] manifest
```

**What it does**

Validates an acceptance run manifest JSON file against `GATE-MATRIX.md` and
the specification held at `--repo-root`. This is the one command dispatched
*before* the global `git`-on-`PATH` check and before `config.json` is
loaded — it exists to gate acceptance claims independently of the rest of
the pipeline. Before this command existed, `validate_run_manifest` had zero
production consumers: only tests called it, and the sole gate test was
itself skipped unless an environment variable was set, so a fully green
suite was fully compatible with the validator never actually running. A
contract nothing invokes gates nothing.

**Options**

| Flag | Type / default | Description |
|---|---|---|
| `-h`, `--help` | flag | Show the command's help message and exit. |
| `manifest` | positional, required | Path to the run manifest JSON to validate. |
| `--repo-root REPO_ROOT` | string, default `.` | Repository root holding `GATE-MATRIX.md` and the specification. |

**Example**

```
$ dreamy verify-run /nonexistent/manifest.json
dreamy: run manifest REJECTED: no run manifest at /nonexistent/manifest.json
```
Exit code: `2`.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | The manifest was validated and admitted — `"verdict": "ADMITTED"` printed. |
| `2` | `ManifestError` — the manifest was rejected. The reason (missing file, schema violation, hash mismatch, containment violation, etc.) is printed as `dreamy: run manifest REJECTED: <reason>`. |

**Notes / gotchas**

- **`verify-run` bypasses the standard `git`-on-`PATH` and config-loading
  checks that gate every other subcommand** — it is validated and returned
  before either of those run, so it works even in an environment that would
  otherwise fail every other `dreamy` command.
- The base directory that acceptance-run manifests are trusted to live
  under is deliberately *not* an exposed parameter of this command — making
  it configurable would let the party making the acceptance claim relocate
  the very trust boundary the containment check exists to enforce.

[↑ Back to top](#table-of-contents)

---

## Validation surfaces (I6–I10)

These are not extra subcommands. They are contracts the product already
enforces — release probes, acceptance-corpus binding, store close semantics,
connector cadence, and the doc-example CLI harness. Operators and harness
authors hit them when publishing, validating acceptance evidence, writing
against `Store`, or reading source health.

### Release-gate probes (I6)

**What it is**

`tools/validation/probe_gates.py` is a real-system probe script (not a
pytest module). It exercises live code paths against temporary git
repositories, a real `Store`, and the real CLI, then prints PASS/FAIL with
what it observed. Under an active Crucible sentinel the project forbids
mock/fixture unit tests for this surface; the probe is the regression
gate.

**How to run**

```
$ .venv/bin/python tools/validation/probe_gates.py
```

The script prints sectioned results to stdout and exits `0` when every
probe PASSes, `1` when any FAILs. Capture stdout under an evidence tree if
you need a durable artifact; the script itself does not write files.

**What it covers (high level)**

Fourteen probes across the I6–I10 surfaces:

| Section | Probes (count) | Asserts |
|---|---|---|
| I6 leak-sweep | 4 | `tools/leak_sweep.py` refuses literal, dash-encoded, and base64-hidden home paths (`exit=1`); a clean control repo passes (`exit=0`). |
| I9 cadence | 3 | A source with a solid baseline and an old watermark is `stale`; a recent watermark is `ok`; below the sample floor is `insufficient data` and never stale. |
| I8 store guard | 2 | `Store.close()` with pending writes raises `RuntimeError`; `commit()` then `close()` does not. |
| I10 doc examples | 1 | Every safe `$ dreamy …` line in README / USAGE / RUNBOOK is accepted by argparse (no `unrecognized arguments` / `invalid choice`). |
| I7 corpus binding | 4 | Workspace instance id is stable; absent and foreign `.produced-by` markers SKIP; a matching marker RUNs. |

I6's own cases target the release path's most expensive class of defect:
a gate that reported green while a dash-encoded or base64-hidden home path
still shipped. The probes assert the **refusals**, not a happy-path push.

**Notes / gotchas**

- The probe constructs temporary repositories and a synthetic `--home`
  (`/Users/probe-operator` inside the script only) so it is correct on any
  machine and does not disclose the operator's real home.
- Mutating / interactive documented examples (`run`, `install`, `tui`, …)
  are classified and counted as skipped, never silently dropped — see I10.

[↑ Back to top](#table-of-contents)

---

### Evidence-corpus binding (I7)

**What it is**

Acceptance gates that compare a machine-local evidence corpus
(`~/.local/share/dreamy/acceptance`) against **this** checkout must refuse
a corpus written by another checkout on the same machine. Without binding,
a fresh clone can fail noisy tracked-tree diffs against a foreign corpus —
noise that trains operators to skip the gate.

**How it works**

1. Each checkout owns a **workspace instance id**: a UUID created on first
   use and stored untracked under the product state tree, keyed by a digest
   of the resolved repo root:

   ```
   ~/.local/share/dreamy/workspace-instances/<sha256(resolved-repo-root)>
   ```

   The file is never committed. A tracked UUID would be identical in every
   clone and could not distinguish two checkouts of the same repo.

2. Whatever writes the acceptance evidence tree should stamp it by calling
   `dreamy.acceptance.write_produced_by(acceptance_root, repo_root)`. That
   writes `acceptance_root/.produced-by` containing this checkout's
   instance id.

3. Gates call `dreamy.acceptance.corpus_binding_skip_reason(acceptance_root,
   repo_root)` before comparing:

   | Marker state | Result |
   |---|---|
   | Missing, empty, or unreadable `.produced-by` | Returns a skip reason (treat as unbound). |
   | Present but id ≠ this workspace | Returns a skip reason naming both ids (foreign corpus). |
   | Present and id matches | Returns `None` — the gate may run. |

Absence is **not** a free pass to compare. Running on an unmarked corpus is
exactly the noise this binding removes; a legacy corpus is unmarked by
definition and SKIPs until a producing pass against this checkout writes
the marker.

**Producer note**

Dreamy's own pipeline does not own the acceptance tree — operators and
external harnesses do. Until a producer calls `write_produced_by`, every
unmarked tree is unbound. The skip reason always names the marker path so
the fix is stated rather than discovered.

There is no CLI flag for this surface; it is a library contract consumed by
acceptance adapters (and exercised by the I7 probes in
`tools/validation/probe_gates.py`).

[↑ Back to top](#table-of-contents)

---

### Store close guard (I8)

**What it is**

`Store.close()` refuses to silently discard uncommitted writes. Closing a
connection that still holds a write transaction used to throw the whole
transaction away with no error and no log line — the writes simply never
happened. The production `run` path is unaffected because `run_pipeline`
commits explicitly; every other caller (scripts, doc seeders, one-off
tools) is exactly who this guard protects.

**Contract**

| Call | Behaviour |
|---|---|
| `store.commit()` then `store.close()` | Closes cleanly. |
| `store.close()` with pending writes, no exception in flight | Rolls the transaction back, closes the connection, then raises `RuntimeError` naming the two exits: `commit()` to keep, or `close(discard=True)` to drop deliberately. |
| `store.close()` with pending writes **and** an exception already propagating (`try` / `finally: store.close()`) | Rolls back and closes **without** raising — so the original traceback is not replaced by the guard. |
| `store.close(discard=True)` | Deliberate teardown: roll back (if needed) and close; never raises for pending writes. |

No pending-statement count is reported. `sqlite3` exposes `in_transaction`
(boolean) and `total_changes` (cumulative for the connection, including
already-committed rows). Neither is a pending count, and inventing one
from `total_changes` would print a wrong number for any caller that
committed earlier in its life.

**Notes / gotchas**

- Committing automatically on close was rejected: it turns an abandoned
  half-write into a durable one.
- Prefer `close(discard=True)` in test/teardown paths that intentionally
  abandon a fixture store.

[↑ Back to top](#table-of-contents)

---

### Connector cadence (I9)

**What it is**

Per-source cadence is a **derived** staleness signal, not a configured
threshold. A source whose watermark is older than `k ×` its own typical
inter-record gap is `stale`. That distinguishes "this harness is idle this
window" from "this harness used to be regular and then stopped" — the case
that usually means a connector broke, moved, or changed format.

Constants (named in `dreamy.cadence`, not operator knobs):

| Name | Value | Role |
|---|---|---|
| `N` | `200` | Last N timestamps feed the median. Large enough that one burst cannot halve it; small enough to track a rhythm that genuinely changed. |
| `K` | `8` | Stale only after missing roughly eight of the source's own typical intervals. |
| `MIN_SAMPLE` | `20` | Below this floor a median is a guess; the source reports `insufficient data` and is never called stale. |

**Where it lives**

- **Schema.** Migration v10 adds `source_cadence` (`source_id` PK,
  `median_gap_ms`, `sample_size`, `computed_ms`). Sample size is stored
  beside the median so a consumer can tell "quiet source with a solid
  baseline" from "new source whose median is a guess". Staleness itself is
  **not** stored — it is re-evaluated on every read against the live
  watermark and wall clock.
- **History.** `Store.cadence_timestamps(source_id, limit)` unions
  `turns.timestamp_ms` and `router_requests.timestamp_ms` (newest first).
  Querying only one table would silently empty the baseline for the other
  path.
- **Ingest.** After each source that yielded records, ingest merges the
  current scan batch with persisted history, keeps the newest `N`, and
  calls `cadence.refresh`. Failures are advisory: they append
  `"{source_id}: cadence refresh failed (...)"` to `stats.warnings` and
  log a warning — they must not fail the run, and they must not fail
  silently.
- **Read path.** `ReadStore.source_stats()` attaches a `cadence` label per
  source: `None` when no baseline has been computed yet; otherwise one of
  `"ok"`, `"stale"`, or `"insufficient data"`.

**Where operators see it**

| Surface | Cadence visible? |
|---|---|
| `dreamy status` CLI JSON | No — that payload is only `latest_run` + `schedule`. |
| Web overview `GET /api/v1/overview` | Yes — each `source_stats[]` entry includes `cadence` (and `ever_ingested`, `record_count`, …). |
| TUI sources panel | Source activity is rendered; the cadence label is available on the underlying `SourceStat` for future panel use. |

**Reading the labels**

| `cadence` | Meaning |
|---|---|
| `null` / absent | No baseline row yet (source never refreshed, or never ingested enough to write one). Distinct from insufficient. |
| `"insufficient data"` | A baseline was computed but the sample is below `MIN_SAMPLE` (or every gap was non-positive). Never treated as stale. |
| `"ok"` | Sample is solid and the watermark is within `K × median_gap`. |
| `"stale"` | Sample is solid and the watermark is older than `K × median_gap`. |

A fixed "stale after N days" rule is deliberately not used: an occasional
harness would sit permanently stale and train operators to ignore the
detector.

[↑ Back to top](#table-of-contents)

---

### Doc-example harness (I10)

**What it is**

The I10 section of `tools/validation/probe_gates.py` extracts every
`$ dreamy …` invocation from:

- `README.md`
- `docs/operations/USAGE.md`
- `docs/operations/RUNBOOK.md`

and asserts the CLI **accepts** it — argparse recognises the subcommand and
every flag. It does **not** require exit code `0`: several documented
invocations demonstrate error paths and exit `2` by design. A renamed flag
then fails a probe instead of a user.

**Classification**

| Class | Treatment |
|---|---|
| Safe, concrete examples | Invoked as `python -m dreamy …` from the repo root; counted as `ran`. Fail if stderr contains `unrecognized arguments` or `invalid choice`. |
| Mutating / interactive (`run`, `install`, `uninstall`, `tui`, `web`, `dismiss`, `undismiss`, `import`, `bundle`) | Counted as `skipped`, never silently dropped. |
| Placeholder args (`UPPER_SNAKE`, `<angle-brackets>`) | Counted as `skipped`. |

**Current surface (verified against this tree)**

```
documented examples accepted by CLI: ran=26 rejected=0 skipped=13
```

Re-check anytime by running the full probe script (see I6) or by grepping
`$ dreamy` examples after a docs edit. Adding a new `$ dreamy …` line to
USAGE / README / RUNBOOK automatically enrolls it in the next probe run —
that is the point.

**Notes / gotchas**

- Doctests were rejected: real timings and paths differ per machine, so a
  doctest is either brittle or so loosely matched it proves nothing.
- Prefer documenting flags that actually exist. If you rename a CLI flag,
  update every `$ dreamy …` example in the three docs above in the same
  change; the probe is what catches the miss.

[↑ Back to top](#table-of-contents)
