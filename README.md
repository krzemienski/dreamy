# dreamy

Read-only cross-harness coding-session reconciler. Dreamy ingests coding
sessions from six sources, correlates them into per-project timelines, and
compiles evidence-cited prompt artifacts you can hand to the next agent run —
no repository mutation, ever.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## What it does

Dreamy watches how you actually work across agent harnesses and turns that
history into something usable.

- **Six sources**: `claude`, `codex`, `nine_router`, `omp`, `opencode`, `pi`.
  Each is an independent connector (`src/dreamy/connectors/`) that discovers
  its native session store, scans within a lookback window, and yields
  normalized records — Nine Router is a request-history source (its own local
  API router), not an agent harness.
- **Correlation**: sessions across harnesses are linked into per-project
  timelines with a confidence tier (`exact`, `high`, `medium`, `unlinked`).
  Unlinked rows are kept, not dropped.
- **Four prompt artifacts** per project, compiled from session evidence
  through installed skill contracts (not written freehand):
  `resumption`, `validation`, `remediation`, `next-tasks`.
- **Three surfaces**: a CLI (`dreamy <command>`), a Textual TUI (`dreamy tui`),
  and a loopback-only web dashboard (`dreamy web`).
- **A scheduler**: `dreamy install` registers a macOS `launchd` job that runs
  `dreamy run` on an interval, so reconciliation happens without you invoking
  it by hand.

## Safety posture

Dreamy is built to be read-only against every repository it observes:

- **No `--apply` flag exists anywhere in the CLI.** There is no escape hatch
  from proposal-only output — every prompt artifact is something you read and
  act on yourself, not something Dreamy executes.
- **No SQL outside the `ReadStore` boundary.** Neither the TUI nor the web
  server issues its own queries; both go through the single typed
  `src/dreamy/read.py::ReadStore`. `dreamy doctor` checks this (`ADR006-1`)
  and currently reports 0 offending files.
- **The web dashboard is loopback-only.** `dreamy web --host` is rejected
  before the socket opens if it resolves to anything other than a loopback
  address. Only `GET`/`HEAD` are served; every other verb returns
  `405 Method Not Allowed` before any handler runs. It makes zero external
  network requests of its own — the favicon, CSS, and JS are all inlined.
- **Secrets are scrubbed, not persisted.** `redact.py` strips known-secret
  values (`NINEROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `OPENAI_API_KEY`) from anything written to disk. `dreamy doctor`'s `N4-1`
  check scans every persisted artifact plus every `state.db` column and
  currently reports 0 secret hits.
- **One unresolved gap, stated plainly:** whether a real Claude Agent SDK run
  is *structurally incapable* of mutation (not just configured not to) is
  still **unverified**. Dreamy applies a fail-closed capability policy
  (`sdk_policy.py`) that sets `tools=[]`, `allowed_tools=[]`,
  `disallowed_tools=[Bash, Edit, Write, NotebookEdit, WebFetch, WebSearch]`
  before any model call — but the live test that would confirm the SDK
  actually refuses those tools in practice
  (`tests/sdk_conformance/test_mutation_denial.py`) is opt-in via
  `DREAMY_RUN_SDK_MUTATION_TEST=1` and is skipped by default, because it
  spends real API money. A skipped test is not evidence. See `SECURITY.md`.

## Requirements

- Python **3.11+** (stdlib-only core — see [Install](#install)).
- **macOS** for the `launchd` scheduler (`dreamy install`/`uninstall`). Every
  other command (`run`, `status`, `findings`, `cost`, `bundle`, `tui`, `web`,
  and the rest) has no macOS-specific dependency in its code path.

## Install

The **durable venv** install is the primary, documented path — install
directly into your checkout is explicitly unsupported for scheduling.

```bash
python3 -m venv ~/.venvs/dreamy
~/.venvs/dreamy/bin/pip install /path/to/dreamer   # non-editable — no `-e`
~/.venvs/dreamy/bin/dreamy status
```

**Why not an editable install?** `dreamy install` refuses to write a
scheduled job pinned to an interpreter that lives inside your source checkout
(verified live):

```text
$ .venv/bin/python -m dreamy install
refusing to install a scheduled job pinned to a non-durable interpreter: /path/to/dreamer/.venv/bin/python lives inside the source checkout /path/to/dreamer; moving, renaming, or deleting that checkout breaks every future run.
The plist outlives this shell, so launchd would fail to spawn with no visible error the next time the job fires.
Install dreamy under a persistent path first, e.g.:
  python3 -m venv ~/.venvs/dreamy
  ~/.venvs/dreamy/bin/pip install <path-to-checkout>
  ~/.venvs/dreamy/bin/dreamy install
(a non-editable install — `-e` leaves the package in the checkout)
[exit 2]
```

A `launchd` plist is persistent state that outlives the shell that wrote it —
if the interpreter it names lives in a repo you might move, rename, or
delete, the schedule silently dies the next time it fires. This check is
lexical on `sys.executable` and the package's own install location, so both
an editable install of the interpreter *and* an editable install of the
package are caught.

### Development install (secondary)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Use this for hacking on Dreamy itself. **`dreamy install` will refuse to run
from this environment** — scheduling is not available from an editable
install; use the durable-venv install above for that.

Optional extras: `agent` (Claude Agent SDK, pinned `0.2.128`, needed for
agent-backed analysis roles) and `tui` (`textual`, pinned `8.2.8`, needed for
`dreamy tui`).

## Quick start

```bash
$ dreamy status
{"latest_run": {"agent_cost_usd": 0.0, ..., "status": "running"}, "schedule": {"installed": true, "interval_seconds": 21600, ...}}

$ dreamy doctor
dreamy doctor — implemented diagnostics (partial; not the full matrix)

  [PASS] STORE-1   R2 state integrity
         integrity_check=ok schema_version=10
  [PASS] R1-1      R1 connector conformance
         6 connectors: claude, codex, nine_router, omp, opencode, pi
  [PASS] R9-1      R9 read-only enforcement
         write refused by SQLite: attempt to write a readonly database
  ...
11 passed, 0 failed, 0 skipped

$ dreamy run --lookback-days 30

$ dreamy findings --limit 3
Findings (3 rows)
  id                state       prov    sev       category        title
  5954cdc9d6a7830c  new         (D)     low       incomplete_work  Project directory no longer exists
  68ec43f9e36c95f8  new         (D)     low       incomplete_work  Project directory no longer exists
  f68177acf29d2458  new         (D)     low       incomplete_work  Project directory no longer exists

$ dreamy bundle <project_id>
```

`dreamy status` and `dreamy doctor` work from any working directory,
including filesystem root — Dreamy resolves entirely from `--config` /
`~/.local/share/dreamy`, never a repo-relative path.

## Command reference

All 18 subcommands. Full worked examples for each: **[USAGE.md](docs/operations/USAGE.md)**.

| Command | Purpose | Details |
|---|---|---|
| `run` | Reconcile real harness stores | [→](docs/operations/USAGE.md#run) |
| `install` | Install/refresh the `com.nick.dreamy` launchd job | [→](docs/operations/USAGE.md#install) |
| `uninstall` | Uninstall the launchd job | [→](docs/operations/USAGE.md#uninstall) |
| `status` | Machine-readable state + schedule status | [→](docs/operations/USAGE.md#status) |
| `skills` | Inspect, refresh, or verify pinned skill bodies | [→](docs/operations/USAGE.md#skills) |
| `tui` | Open the Textual dashboard | [→](docs/operations/USAGE.md#tui) |
| `web` | Serve the read-only loopback dashboard | [→](docs/operations/USAGE.md#web) |
| `verify-run` | Validate an acceptance run manifest (exit 0 admits, 2 rejects) | [→](docs/operations/USAGE.md#verify-run) |
| `cost` | Cost rollup; `--project` scopes to one project, `--window daily\|monthly` reports the cumulative-cap window | [→](docs/operations/USAGE.md#cost) |
| `bundle` | Export a self-contained project HTML bundle | [→](docs/operations/USAGE.md#bundle) |
| `efficacy` | Prompt efficacy report — `not_observed` is never a failure | [→](docs/operations/USAGE.md#efficacy) |
| `findings` | Findings; default state filter is `new`+`regressed`; `--metrics` reports per-category lifecycle rates | [→](docs/operations/USAGE.md#findings) |
| `dismiss` | Dismiss a finding with a reason code | [→](docs/operations/USAGE.md#dismiss) |
| `undismiss` | Reverse a previous dismissal | [→](docs/operations/USAGE.md#undismiss) |
| `doctor` | Run the implemented acceptance diagnostics (partial matrix) | [→](docs/operations/USAGE.md#doctor) |
| `diff` | Diff findings between the latest two runs | [→](docs/operations/USAGE.md#diff) |
| `import` | Import a bundle as an archival snapshot (not a project restore) | [→](docs/operations/USAGE.md#import) |
| `archives` | List imported bundle snapshots | [→](docs/operations/USAGE.md#archives) |

### Reconciliation

`dreamy run` scans every configured source within `--lookback-days`,
correlates the results, and persists episodes, findings, and prompt
artifacts. `--source-path SOURCE=PATH` overrides a connector's discovered
path (e.g. for testing against a fixture directory); `--agent-cap-usd` sets a
one-off positive spend cap for agent-backed analysis roles for that run only.

### Scheduling

`install`/`uninstall`/`status` manage and inspect the `launchd` job. See
[Install](#install) for the durable-venv requirement.

### Findings lifecycle

`findings`, `dismiss`, `undismiss`, and `diff` all operate on the same delta
classification (`new`, `persisting`, `resolved`, `regressed`, `dismissed`).
`dismiss` requires one of four reason codes: `wrong`, `wont-fix`,
`already-done`, `duplicate`. A dismissal is reversible and only sticks until
the underlying evidence materially changes.

### Reporting and export

`cost`, `efficacy`, and `bundle` turn accumulated run history into
consumable output — a spend rollup, a per-artifact-type usage report, and a
single self-contained HTML file per project respectively.

### Interactive surfaces

`tui` opens the Textual dashboard (requires the `tui` extra). `web` serves
the loopback-only dashboard described in [Safety posture](#safety-posture);
routes are documented in [WEB-DASHBOARD.md](docs/operations/WEB-DASHBOARD.md).

### Verification

`verify-run` validates an acceptance run manifest against the repository's
gate matrix and exits 0 (admit) or 2 (reject). `doctor` runs the currently
implemented subset of acceptance diagnostics — 10 checks as of this writing,
not the full matrix (see [GATE-MATRIX.md](docs/acceptance/GATE-MATRIX.md)).

## Configuration

Config is read from `~/.local/share/dreamy/config.json` by default (override
with `--config <path>`; TOML is accepted read-only, JSON is the write
format). Missing file means all defaults. Unknown keys are ignored, so a
config written for a newer build still loads on an older one. Every key
below is a real field on `dreamy.config.Config`
(`src/dreamy/config.py`) — none are invented:

| Key | Default | Purpose |
|---|---|---|
| `interval_seconds` | `21600` (6h) | Seconds between scheduled `launchd` runs. Must be a positive `int`. |
| `lookback_days` | `30` | How far back a run scans. |
| `output_dir` | `~/.local/share/dreamy` | Working directory for `state.db`, artifacts, and logs. |
| `correlation_window_seconds` | `30` | T2 correlation tolerance: a commit within this window of a session may correlate to it. |
| `analysis_workers` | `4` | Worker pool size for per-project analysis. |
| `spend_cap_usd` | `None` | Per-run agent spend cap. `None` disables agent analysis entirely — this is the safe default. Must be positive if set. |
| `spend_warn_usd` | `None` | Soft warn threshold; never halts a run. |
| `retention_days` | `90` | Artifacts older than this are deleted. |
| `include_projects` | `[]` | Non-empty = allowlist of project ids. |
| `exclude_projects` | `[]` | Applied after `include_projects`. |
| `goals_paths` | `[]` | Extra files scanned for goal markers. |
| `agent_model` | `"cc/claude-opus-5"` | Model used for agent-backed analysis roles. |
| `ninerouter_base_url` | `"http://localhost:20128"` | Nine Router local API endpoint. |
| `claude_cli_path` | `None` | Absolute path to the Claude CLI; omit to resolve from `PATH`. |
| `ninerouter_api_key_env` | `"NINEROUTER_API_KEY"` | Env var *name* holding the Nine Router key — never the key itself. |
| `agents_enabled` | `{research: false, teacher: false, friends: false, compiler: true}` | Only the deterministic compiler role is on by default. |
| `log_level` | `"DEBUG"` | Logging level. |
| `log_topics` | `["ingest", "correlate", "analyze", "agent", "research", "teacher", "friends", "compiler", "report", "schedule"]` | Which topics get logged. |
| `emit_mode` | `"inline"` | `"inline"` embeds pinned skill bodies (byte-identical reruns); `"native"` references them instead. |
| `skill_pin` | `true` | Pin skill bodies by SHA-256; `dreamy skills status`/`refresh` manage drift. |
| `source_paths` | `{}` | Per-source path overrides, keyed by `SOURCE_ID` (`claude`, `codex`, `nine_router`, `omp`, `opencode`, `pi`). |
| `prompt_chains` | `{}` | Per-artifact-type overrides of the default skill chains. A named chain replaces its default outright. |

A fully commented example lives at
[`config/dreamy.example.toml`](config/dreamy.example.toml) — it is loaded by
the production validator in CI, so it cannot silently drift from the code.

## How it works

Each run moves through five stages: **ingest** the six sources within the
lookback window → **correlate** records into per-project timelines by
confidence tier → **analyze** each project (deterministic + optional
agent-backed roles) → **compile** the four prompt artifacts through pinned
skill chains → **report** findings, costs, and efficacy for the CLI, TUI, and
web dashboard to read back out.

## Documentation map

| Directory | For |
|---|---|
| [`docs/operations/`](docs/operations/) | Runbook, TUI, web dashboard, release process |
| [`docs/architecture/`](docs/architecture/) | System architecture, observability, agent pipeline, and the [improvement roadmap](docs/architecture/IMPROVEMENTS-I6-I10.md) |
| [`docs/decisions/`](docs/decisions/) | ADRs — decisions expensive to reverse, with rejected alternatives |
| [`docs/acceptance/`](docs/acceptance/) | Gate matrix and acceptance results |
| [`docs/security/`](docs/security/) | Threat model and permission policy |
| [`docs/research/`](docs/research/) | SDK version lock and capability matrix |
| [`docs/specifications/`](docs/specifications/) | Product spec and build history |
| [`src/dreamy/`](src/dreamy/) | Implementation. `protocol.py` defines the connector contract every source implements. |

`SECURITY.md` and `CONTRIBUTING.md` at the repository root cover
vulnerability reporting and how to contribute.

## License

[MIT](LICENSE) © 2026 Nick Krzemienski
