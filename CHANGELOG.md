# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every entry carries the requirement IDs from
`docs/specifications/SPEC-DREAMY-V2.md` that it satisfies or changes.

Dreamy's source is published publicly on GitHub as a squash-release series on
`main` (one commit per release, no tags, no PyPI artifact). There is exactly
one section below. `2.0.0` is the first intended packaged release;
`pyproject.toml` currently declares `2.0.0.dev0`.

## [Unreleased]

### Added

- **Installable product layout** `PKG-001` — runtime code moved from
  `scripts/dreamy/` to `src/dreamy/`, with `pyproject.toml`, package metadata,
  and a `dreamy` console entry point. `scripts/` now holds only maintainer
  wrappers and ships in no artifact.
- **Packaged skill lock** `BOOT-002` `PKG-002` — `skills.lock` is resolved
  through `importlib.resources` from inside the wheel. `config/skills.lock` is a
  symlink to the packaged copy, so the editable checkout and the installed
  product cannot diverge.
- **`tools/regen-skills-lock.py`** `BOOT-002` — regenerates the lock from
  resolved skills, with `--check` for CI. Deliberately not a `dreamy`
  subcommand: the product verifies the lock and must never rewrite it.
- **Configuration examples** — `config/dreamy.example.toml` and
  `config/prompt-chains.example.toml`, both loaded through the production
  validator by tests so they cannot drift from the loader.
- **ADR-009** `BOOT-002` `SDK-009` `PKG-002` — skill resolution, pinning,
  hashing, and missing-skill behaviour.
- **ADR-010** `SDK-002` `SDK-003` `SDK-005` `SDK-010` — the rule that assigns
  every SDK capability a disposition, plus a measured table of which `required`
  capabilities are wired.
- **`dreamy verify-run <manifest>`** `REL-001` — CLI consumer for the
  run-manifest validator; exit 0 admits, exit 2 rejects. Until now the
  validator was reachable only from tests, so a contract with zero production
  consumers was gated only by its own test pass.
- **`dreamy cost`** `R10` — CLI rollup of attributed and unattributed agent
  spend with confidence mix and per-project breakdown; `--project <id>` scopes
  to one project's 30-day cost; `--json` for machine-readable output. The
  unattributed bucket is reported separately, never silently folded into the
  attributed total.
- **`dreamy bundle <project_id>`** `R14` — exports a single self-contained
  HTML bundle for one project (report, all four prompts, evidence index, cost
  rollup, recurring goals); CSS+JS inlined, no external assets, canary-scan
  guard refuses to emit if a secret would leak into the rendered bytes.
- **`dreamy efficacy`** `R21` — per-artifact-type efficacy report.
  `not_observed` is an honest bucket (the artifact was never seen in a later
  transcript) and is never counted as a failure; the report makes the
  distinction explicit.
- **`dreamy findings`** `R13` — per-finding table with delta state, severity,
  category, provenance (`(A)` agent vs `(D)` deterministic). Default state
  filter is `new+regressed` (rows that need operator attention); `--state` is
  repeatable; `--state all` disables filtering. `--severity`, `--category`,
  `--project`, `--limit`, `--json` for slicing and machine output.
- **`dreamy dismiss` / `dreamy undismiss`** `R20` — dismissal takes a
  mandatory reason from the four R20 codes (`wrong`, `wont-fix`,
  `already-done`, `duplicate`); undismiss reverses. Both write through
  `ReadStore` so the change is reflected immediately in the TUI, the web
  dashboard, and the next CLI query.
- **`dreamy diff`** `R13` — diff between the latest two runs: new, persisting,
  resolved, regressed, dismissed counts plus per-finding rows. `--json` for
  machine-readable output.
- **`dreamy web --host 127.0.0.1 --port 8765`** `R17` — stdlib
  `ThreadingHTTPServer` serving the dark-hybrid dashboard. `--host` must
  resolve to loopback; non-loopback bind is refused at startup, before any
  request is served. GET/HEAD only; mutations return 405. Per-request
  `ReadStore`, no shared SQLite connection, CSP/nosniff/no-referrer on every
  JSON response, no external network requests. See ADR-011.
- **SDK capability policy** `SDK-003` `SDK-004` — `sdk_policy.py` (named
  `dreamy-readonly-evidence-summarizer-v1`) applies a fail-closed policy on
  `ClaudeAgentOptions`: `tools=[]`, `allowed_tools=[]`,
  `disallowed_tools=[Bash, Edit, Write, NotebookEdit, WebFetch, WebSearch]`,
  `permission_mode='dontAsk'`, `setting_sources=[]`. The policy is pinned to
  `claude-agent-sdk==0.2.128`; any other installed version raises
  `RuntimeError` before any call. Missing SDK fields raise `RuntimeError`
  rather than degrading silently.
- **Bounded deterministic parallelism** — `orchestration.py` (`map_units`)
  runs per-project units through a `ThreadPoolExecutor` with
  `max_workers=cfg.analysis_workers` (default `4`); `analysis_workers=1`
  deliberately bypasses the executor so it remains the exact sequential
  reference mode. Results return as immutable `ResultEnvelope`s sorted by
  `unit_id`; the coordinator persists in that order. Parallel and sequential
  runs produce equivalent normalized outputs; completion order is never
  observable to callers. See ADR-012.
- **TUI `[7] NEXT RUN` view** — project include/exclude list editor that
  writes back through the existing atomic config writer. The change takes
  effect on the next scheduled run, not the currently displayed one — current
  display filtering must not masquerade as run configuration.
- **`analysis_workers` config key** — positive integer, default `4`,
  validated by `config.py` (`> 0`, type-checked against `int` to reject
  `bool`). `1` is the documented sequential reference mode.

### Fixed

- **`prompt_chains` was documented as configurable and was not** — the value was
  read with `getattr(cfg, "prompt_chains", None)` against a dataclass that had
  no such field, so every user override was silently discarded. The field now
  exists and the override reaches the compiler.
- **Skill lock resolved relative to the working directory** `PKG-002` — the
  default was `config/skills.lock`, which succeeded from the checkout and raised
  `no skill lock at config/skills.lock` from anywhere else. This is the worst
  available failure mode: invisible in development, broken on delivery.
- **`emit_mode='native'` is no longer silently ignored** `R6f` — the compiler
  now recognizes `native` as a reserved value: the inline artifact remains
  the source of truth, and `native` additionally appends a bounded
  `dreamy-native-hint` trailer naming the resolved skills plus a one-shot
  `RuntimeWarning` so anyone who opts in to native is told plainly that the
  inline artifact is the source of truth. The earlier "not implemented"
  wording in `SKILL.md` was correct that no harness-aware emit ships today
  but inaccurate about what the compiler does on `native`.
- **Cost rollup previously hid a 90k-row unattributed bucket** `R10` —
  `dreamy cost` now shows the unattributed total alongside the attributed
  one (and prints the request count contributing to it). Most of the
  unattributed spend is real, not noise; folding it into the attributed
  total masked where it came from.
- **R13 default findings view surfaced unchanged rows** `R13` — the default
  `findings` filter is now `new+regressed`; unchanged and dismissed rows
  remain discoverable via `--state all`. Both `dreamy findings` and
  `dreamy diff` share one classifier (`ReadStore._delta_states`) so a finding
  reads the same way in the list and the diff.
- **R13 classification now derived from a per-project observation ledger**
  `R13` — `findings.state` was previously a stored column written once at
  detection, which meant a finding that resolved on a later project run kept
  reading as `new` until the next project-level run reclassified it. The new
  derivation compares current observations against the last project-level run
  that touched the finding's project, so the diff view tracks the project
  run, not a global one. The earlier mismatch was subtle: consecutive runs
  covering different projects produced the same finding reading `persisting`
  in one place and `regressed` in another.
- **SDK capability posture is now fail-closed** `SDK-003` — previously the
  call site set only `output_format` and `system_prompt`; permissions, hooks,
  MCP servers, custom tools, subagents, and skills/setting sources were all
  unset (which means unset, not denied). A pinned policy module now rejects
  at startup if any required field is missing from the installed SDK
  version, and applies the read-only denylist before any call.

### Changed

- **Naming migration to `dreamy`** `PKG-001` `PKG-003` — `continuity` and
  `cross-harness-continuity` are retired as identifiers across the package
  directory, launchd label, database filename, environment variables, CLI help,
  and documentation. See ADR-008.
- **`SKILL.md` updated to operator format** — the previous version described
  the obsolete `scripts/` layout. The package is installed and the `dreamy`
  entry point is on PATH; `scripts/` is gone. New section for the web
  dashboard routes, the dismiss reason vocabulary, and `analysis_workers`
  semantics.
- **`README.md` updated to P1 surface** — every new command documented with
  real output captured from the installed binary; web dashboard documented
  as a loopback-only, GET/HEAD surface with the full route table.

### Security

- **Store-boundary redaction** `SAFE-002` — agent output reached SQLite
  unredacted. Redaction now happens at `Store.finish_agent_call`, proven by a
  test that scans the database file on disk rather than the in-memory value.

### Known gaps

Recorded here because a changelog that lists only progress misrepresents state.

- **`SAFE-001` is unverified, not satisfied** — the AST scan now covers more
  call sites (`sdk_policy.apply()` is the only mutating handshake with the
  SDK), but the test that would settle it
  (`tests/sdk_conformance/test_mutation_denial.py`) is opt-in
  (`DREAMY_RUN_SDK_MUTATION_TEST=1`) and currently skipped. The policy is
  what proves the contract at *call* time; a real Claude Code agent run
  that bypasses our wrapper could still mutate. A skipped test is not
  evidence.
- **Eight custom tools specified but not built** `SDK-004` — the read-only
  Dreamy MCP tools named in `AGENT-PIPELINE.md` (per-session excerpts, project
  session lists, git evidence, findings, artifact manifest, evidence path
  resolution, skill manifest, human-question submission) do not exist as a
  registered server. The agent layer is now fail-closed (`tools=[]`), so the
  absence is the right posture today, but it is still a gap against the spec.
- **Four required SDK capabilities unwired** `SDK-003` — `permission_mode`
  and `setting_sources` are now wired through `sdk_policy.py`; hooks, MCP
  servers, custom tools, and subagents remain spec-only. The disposition rule
  in `ARCHITECTURE.md` ADR-010 records this.
- **`emit_mode='native'` is a reserved stub** `R6f` — the compiler
  recognizes the value and emits an inline artifact plus a trailer and a
  warning; a real harness-aware emit (e.g. emit Claude Code `Skill(`
  invocations for that harness, inline for the rest) has not shipped.
- **TUI has seven views, web dashboard has six** `R12` — the TUI gained the
  `[7] NEXT RUN` project include/exclude editor; the web dashboard covers the
  other six. The web dashboard's project include/exclude is reachable via
  `GET /api/v1/projects/{id}` but not yet editable through the dashboard
  itself.
- **G2 packaging evidence was destroyed** — four artifacts in
  `run-260801-180000-phase00-foundation` were captured under a false premise and
  then overwritten in place, invalidating the reviewer verdicts bound to their
  hashes. Eight of twelve survive at their recorded hashes. See
  `INCIDENT-260801-cwd-contamination.md`. The re-captured run is `PENDING` and
  awaits an independent reviewer.
- **All acceptance gates G0–G9 are BLOCKED.** See
  `docs/acceptance/GATE-MATRIX.md` for what each gate now needs.