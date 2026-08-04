---
name: dreamy
description: Read-only cross-harness coding-continuity reconciler that ingests the trailing 30-day window from OMP, Pi, Claude Code, Codex, OpenCode, and Nine Router, correlates them into per-project timelines, runs deterministic and optional agent analysis, and compiles evidence-cited resumption, validation, and remediation prompts. Runs on demand or every 6 hours via the `com.nick.dreamy` launchd job.
version: 1.1.0
---

# Dreamy — Cross-Harness Continuity Reconciler

Dreamy is the read-only cross-harness coding-continuity reconciler (formerly `cross-harness-continuity`, retired by ADR-008). It ingests the trailing 30-day window from five agent harnesses plus the Nine Router, correlates them into per-project timelines, runs deterministic analysis on git and prompt-tape evidence (with an optional agent layer on top), and compiles evidence-cited resumption, validation, and remediation prompts.

## Quick Start

The package is installed. The `dreamy` entry point is on PATH inside that
environment:

```bash
# Run from anywhere (filesystem root, another repo, /tmp); no PYTHONPATH or
# repo-relative imports. PKG-003 requires the command to resolve from any
# working directory.
dreamy status

# One-time setup: install schedule (every 6 hours, reboot-safe)
dreamy install

# Manual run over trailing 30 days
dreamy run

# Wider lookback for ad-hoc walks
dreamy run --lookback-days 90

# Temporary positive per-run agent spend cap (overrides config spend_cap_usd)
# dreamy run --agent-cap-usd 0.10

# Override a single source path for this run (fault injection or custom store)
dreamy run --source-path nine_router=/tmp/test.db

# TUI dashboard
dreamy tui

# Loopback web dashboard (R17) — stdlib server, GET/HEAD only
dreamy web --host 127.0.0.1 --port 8765

# P1 CLI surfaces (R10 cost, R13 findings/diff, R14 bundle, R20 dismiss,
# R21 efficacy). All accept --json.
dreamy cost [--project <id>] [--json]
dreamy bundle <project_id> [--output <path>]
dreamy efficacy [--json]
dreamy findings [--state <s>] [--severity <s>] [--category <c>] [--project <id>] [--limit N] [--json]
dreamy dismiss <finding_id> --reason {wrong,wont-fix,already-done,duplicate}
dreamy undismiss <finding_id>
dreamy diff [--json]

# Uninstall schedule
dreamy uninstall

# Re-pin skill content hashes after editing a SKILL.md (Q11 resolution)
dreamy skills refresh
# Just inspect pin status without re-pinning
# dreamy skills status
```

## What It Does

1. **Ingests** sessions from OMP, Pi, Claude Code, Codex, OpenCode JSONL/SQLite stores plus Nine Router request history — all read-only.
2. **Correlates** cross-harness sessions into per-project work timelines using exact keys and confidence-scored fuzzy matching.
3. **Analyzes** original intent, completion status, drift episodes, major code changes, tech debt, and documentation drift using git evidence and transcript signals. Per-project analysis runs through `map_units` with bounded parallelism (`analysis_workers`); `analysis_workers=1` is the exact sequential reference mode.
4. **Generates** resumption prompts, functional validation prompts, remediation prompts, and next-task proposals — all evidence-cited and proposal-only.
5. **Reports** centrally to `~/.local/share/dreamy/` with per-project Markdown, JSON, and HTML.
6. **Serves** a loopback-only read-only HTTP dashboard (`dreamy web`) with the same `ReadStore` boundary used by the TUI. No mutation routes, no remote bind.

## Configuration

Config file: `~/.local/share/dreamy/config.json`

```json
{
  "interval_seconds": 21600,
  "lookback_days": 30,
  "output_dir": "~/.local/share/dreamy",
  "retention_days": 90,
  "correlation_window_seconds": 30,
  "analysis_workers": 4,
  "spend_cap_usd": null,
  "spend_warn_usd": null,
  "include_projects": [],
  "exclude_projects": [],
  "goals_paths": [],
  "agent_model": "cc/claude-opus-5",
  "ninerouter_base_url": "http://localhost:20128",
  "ninerouter_api_key_env": "NINEROUTER_API_KEY",
  "agents_enabled": {"research": false, "teacher": false, "friends": false, "compiler": true},
  "skill_pin": true,
  "emit_mode": "inline"
}
```

Notable keys:

- `spend_cap_usd` — **per-run** USD cap on agent analysis. `null` (default) disables agent analysis until a positive cap is set explicitly. Cost is always recorded in the `agent_calls` table. An optional `spend_warn_usd` soft threshold logs amber without halting.
- `goals_paths` — ordered list of declared long-term goals files (default is empty; operator populates per project, e.g. `"goals_paths": ["path/to/GOALS.md"]`). Declared goals outrank recurring intent inferred from sessions.
- `analysis_workers` — per-project parallelism for the deterministic analysis stage (positive integer, default `4`). Wired into `orchestration.map_units(max_workers=cfg.analysis_workers)`. The sequential reference mode is `1`, which deliberately bypasses the executor so it remains the byte-identical fallback for parity checks; any value `> 1` runs a `ThreadPoolExecutor` with one SQLite writer per worker, and the coordinator persists in sorted-by-`unit_id` order so parallel and sequential runs produce the same per-project output. The `unit_id` is `project_path`, so output order is deterministic regardless of completion order.
- `skill_pin` — when `true` (default), the compiler pins a SHA-256 snapshot of each inlined skill body per artifact so unchanged runs produce byte-identical artifacts. `dreamy skills refresh` re-pins to the current on-disk content.
- `emit_mode` — `inline` (default) pastes the skill constraint blocks verbatim into the artifact. `native` is **recognized but reserved for a future harness-aware emit**: the inline artifact is the source of truth, and selecting `native` additionally appends a bounded `dreamy-native-hint` trailer naming the resolved skills plus a one-shot `RuntimeWarning`. Today every artifact is the inline form. See `SPEC-addendum-prompts-p1p2-tui.md` §R6f.

## Source Paths

| Source | Default Path | Format |
|--------|-------------|--------|
| Nine Router | `~/.9router/db/data.sqlite` | SQLite (`usageHistory`) |
| OMP | `~/.omp/agent/sessions/` | JSONL |
| Pi | `~/.pi/agent/sessions/` | JSONL |
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite |

Override any source path per-run: `--source-path nine_router=/tmp/test.db`

## Safety

- All managed repositories are **read-only** — no source-tree mutations in any mode.
- Documentation changes are **proposal-only** unified diffs in the output directory.
- Secrets are redacted at the ingestion boundary before persistence.
- State lives outside managed repos under `~/.local/share/dreamy/`.
- Single-instance lock prevents concurrent runs.
- The agent layer applies a fail-closed SDK capability policy
  (`sdk_policy.py`): `tools=[]`, `allowed_tools=[]`,
  `disallowed_tools=[Bash, Edit, Write, NotebookEdit, WebFetch, WebSearch]`,
  `permission_mode='dontAsk'`, `setting_sources=[]`, pinned to
  `claude-agent-sdk==0.2.128`. A pinned version mismatch raises
  `RuntimeError` before any call.
- The web dashboard binds to loopback only — `--host` must resolve to a
  loopback address; `0.0.0.0` is refused at startup. Mutations are 405. Every
  JSON response carries CSP `default-src 'self'`, `nosniff`, `no-referrer`,
  and `Cache-Control: no-store`.

`SAFE-001` (a real Claude Code agent run cannot mutate managed repositories)
remains **UNVERIFIED**: the test that would settle it
(`tests/sdk_conformance/test_mutation_denial.py`) is opt-in
(`DREAMY_RUN_SDK_MUTATION_TEST=1`) and skipped. See `SECURITY.md`.

## Output Layout

```
~/.local/share/dreamy/
├── state.db                    # SQLite state store
├── config.json                 # Configuration
├── runs/                       # Per-run logs
│   └── {run_id}.jsonl
├── reports/
│   ├── latest/                 # Most recent run
│   │   ├── executive-report.md
│   │   ├── executive-report.json
│   │   ├── executive-report.html
│   │   └── projects/
│   │       └── {project_slug}/
│   │           ├── report.md
│   │           └── prompts/
│   │               ├── resumption.md
│   │               ├── validation.md
│   │               ├── remediation.md
│   │               └── next-tasks.md
│   └── archive/                # Dated snapshots
├── bundles/                    # R14 self-contained project bundles (--bundle)
│   └── {project_id}.html
├── logs/                       # launchd stdout / stderr
│   ├── launchd.out.log
│   └── launchd.err.log
└── lock                        # Single-instance lock
```

## Web Dashboard Routes (R17)

`dreamy web --host 127.0.0.1 --port 8765` serves these read-only routes. Every
request opens its own `ReadStore`. `POST`/`PUT`/`PATCH`/`DELETE` return `405
Method Not Allowed`.

| Route | Purpose |
|---|---|
| `GET /healthz` | `{"status":"ok","has_state":true}` |
| `GET /api/v1/overview` | Latest run + per-source availability |
| `GET /api/v1/runs?limit=N` | Run history |
| `GET /api/v1/findings?project=&severity=&category=&state=` | Findings (default `state=new,regressed`) |
| `GET /api/v1/projects` | Project list |
| `GET /api/v1/projects/{id}` | One project's episodes, prompts, costs, findings |
| `GET /api/v1/prompts?project=&type=` | Compiled prompt artifacts |
| `GET /api/v1/schedule` | Schedule state |
| `GET /api/v1/monitor?run_id=&limit=N` | Live stage/log stream |

## Adding a New Harness Connector

1. Create `src/dreamy/connectors/newharness.py`.
2. Implement the `Connector` protocol from `src/dreamy/protocol.py`.
3. Set `SOURCE_ID`, `default_source_path()`, `discover()`, and `scan()`.
4. Register in `src/dreamy/connectors/__init__.py`.

## Dependencies

Python 3.11+ standard library plus the pinned `claude-agent-sdk==0.2.128` (used
only when `spend_cap_usd > 0`). The TUI ships in the `tui` extra
(`textual==8.2.8`). Uses system `git` CLI for repository evidence.

## Troubleshooting

- **"git binary not found"**: Install git or add to PATH. Git evidence is a hard dependency.
- **"source not found" warnings**: Normal when a harness isn't installed. The run continues with available sources.
- **Slow first run**: Initial ingestion scans all trailing-window files. Subsequent runs use watermarks for incremental processing.
- **`SAFE-001` UNVERIFIED in `dreamy status`**: expected — the test is opt-in. To prove it: `DREAMY_RUN_SDK_MUTATION_TEST=1 pytest tests/sdk_conformance/test_mutation_denial.py`.
- **Web server refuses to start**: `--host` resolved to a non-loopback address. Use `127.0.0.1` or `::1`; the operator contract is loopback-only.