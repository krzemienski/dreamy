# dreamy — Architecture Decision Records

**Project:** dreamy
**Date:** 2026-07-30
**Deciders:** Nick (sole)
**Related:** `DESIGN.md`, `SPEC-dreamy.md`, `TASK.md`

Nine ADRs covering the decisions that are expensive to reverse. Decisions cheap to change later (log format, report styling, keybindings) are deliberately absent — an ADR for a reversible choice is bureaucracy.

| ADR | Decision | Status |
|---|---|---|
| [001](#adr-001) | SQLite for state | Accepted |
| [002](#adr-002) | Connector ABC over a plugin registry | Accepted |
| [003](#adr-003) | Agent as optional layer, not required stage | Accepted |
| [004](#adr-004) | Proposal-only writes, no escape hatch | Accepted |
| [005](#adr-005) | launchd `StartInterval` over `StartCalendarInterval` | Accepted |
| [006](#adr-006) | Read layer between state and TUI | Accepted |
| [007](#adr-007) | Project identity on git remote + path, from day one | Accepted |
| [008](#adr-008) | Rename to dreamy; retire `cross-harness-continuity` | Accepted |
| [009](#adr-009) | Inline skill contracts rather than reference them | Accepted |

---

<a name="adr-001"></a>
## ADR-001: SQLite for state

**Status:** Accepted · **Date:** 2026-07-30

### Context

dreamy needs durable state for ~500 sessions, ~50k turns, and derived findings over a rolling 30-day window, on one machine, for one user. Two processes touch it: the reconciler (writer) and the TUI (reader). The project constrains its core to Python 3.11 stdlib with no pip installs.

### Decision

SQLite in WAL mode at `~/.local/share/dreamy/state.db`, accessed through the stdlib `sqlite3` module. Numbered migrations. Single writer enforced by an `fcntl` lock outside the DB.

### Options Considered

#### Option A: SQLite (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Low — stdlib, zero install, one file |
| Cost | Zero |
| Scalability | 3+ orders of magnitude above need |
| Team familiarity | High — already used by 9router and OpenCode |

**Pros:** No dependency, no daemon; WAL gives concurrent reads during a run; atomic transactions make interrupted runs safe; a single file makes backup and reset trivial; already proven in this exact domain by the sources themselves.
**Cons:** One writer; weak typing; no native JSON operators in older builds.

#### Option B: PostgreSQL

| Dimension | Assessment |
|---|---|
| Complexity | High — server, service management, connection config |
| Cost | Nonzero: RAM, a process, ongoing maintenance |
| Scalability | Far beyond need |
| Team familiarity | High |

**Pros:** Real types, concurrent writers, JSONB, rich indexing.
**Cons:** A background server for 50k rows on a laptop. Breaks the zero-dependency constraint. Adds a failure mode ("is postgres running?") to a tool whose value is that it silently works.

#### Option C: Flat files (JSONL + JSON indexes)

| Dimension | Assessment |
|---|---|
| Complexity | Deceptively high |
| Cost | Zero |
| Scalability | Poor for the query patterns |
| Team familiarity | High |

**Pros:** Human-inspectable; trivially diffable; no schema migrations.
**Cons:** Every query becomes a full scan. Watermarks, joins, and idempotent upserts must be hand-built — which is reimplementing a database badly. No atomicity, so an interrupted run can leave torn state.

#### Option D: DuckDB

**Pros:** Excellent analytical performance; good Parquet story.
**Cons:** A pip dependency, violating the core constraint. Optimized for scans over millions of rows — a workload this does not have. Solving a performance problem that does not exist.

### Trade-off Analysis

The only genuine cost of SQLite is single-writer. That cost is zero here, because the design already mandates a single writer via `fcntl` for reasons unrelated to storage — concurrent reconciliation runs would produce garbage regardless of the backend.

Everything else favors SQLite decisively. The data is small, the access pattern is one writer and one reader, the deployment target is a laptop, and the sources are themselves SQLite files. Postgres and DuckDB solve scale problems this project does not have while breaking a constraint it does have.

Flat files deserve mention because they look simpler and are not. The moment you need "records newer than watermark for source X, joined to project, ordered by time," you are writing a query planner.

### Consequences

**Easier:** Zero-install deployment. Trivial backup (`cp state.db`). Reset is `rm`. The TUI reads live during a run via WAL. Interrupted runs resume cleanly on transaction boundaries.

**Harder:** Schema changes need explicit migrations — and `store.py` currently has no ladder, only a v1 bootstrap. That is now a blocking gap. Concurrent writes are impossible, so any future parallel ingest must marshal writes through one thread.

**Revisit when:** A second machine needs shared state (ADR-007), or the window grows past ~5M turn rows.

### Action Items

1. [ ] Replace `store.py::_migrate()` v1 bootstrap with a numbered migration ladder **before any schema change ships**
2. [ ] Add `PRAGMA integrity_check` on open; refuse to run on a corrupt DB
3. [ ] Add the columns in DESIGN.md §3.2 as migration 002

---

<a name="adr-002"></a>
## ADR-002: Connector ABC over a plugin registry

**Status:** Accepted · **Date:** 2026-07-30

### Context

Six sources in three shapes (JSONL directories, SQLite files, nested project directories), owned by six external projects free to change format without notice. More harnesses will appear. `protocol.py` already defines a `Connector` ABC; this ADR ratifies it and rejects the alternatives explicitly.

### Decision

A Python ABC with four members — `SOURCE_ID`, `default_source_path()`, `discover()`, `scan()` — implemented identically by all six, registered in a static list in `connectors/__init__.py`.

### Options Considered

#### Option A: Static ABC + explicit registration (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Cost | Zero |
| Scalability | Linear in sources; fine |
| Team familiarity | High |

**Pros:** Type-checkable; every connector is discoverable by reading one list; a new source is one file plus one line; no import magic; trivially testable in isolation via `path_override`.
**Cons:** Adding a source requires editing a file (which is not actually a cost for a solo project).

#### Option B: Entry-point plugin registry

**Pros:** Third parties could ship connectors without touching the repo.
**Cons:** There are no third parties. Adds packaging machinery and import-time failure modes to serve a constituency of one. Debugging "why didn't my connector load" is strictly worse than reading a list.

#### Option C: Config-driven declarative connectors (paths + field mappings in JSON)

**Pros:** New sources without code. Appealing on the surface.
**Cons:** Each of the six formats needs genuinely different logic — OMP encodes cwd in directory names with a lossy transform, 9router is a SQL table with ISO text timestamps, Claude Code nests per-project. A config language expressive enough to cover all six *is* a programming language, and a worse one than Python.

### Trade-off Analysis

Option C is the seductive one and the trap. "Just describe the format in JSON" works until the second format, at which point the config schema starts growing conditionals. The formats are not variations on a theme; they are unrelated.

Option B optimizes for an ecosystem that will not exist.

Option A's only cost — editing a registration list — is not a cost. It is a feature: one file answers "what sources exist."

### Consequences

**Easier:** New connector = one file + one line. Fault injection via `path_override` needs no framework. A format change is contained to one file. Everything above the connector layer is source-agnostic.

**Harder:** Six near-identical `discover()` implementations. Mitigated by the existing `_JsonlConnector` base that OMP and Pi already share — Claude Code and Codex should evaluate the same base before duplicating.

**Revisit when:** Someone other than Nick wants to ship a connector.

### Action Items

1. [ ] Evaluate whether Claude Code and Codex can extend `_JsonlConnector`
2. [ ] Add a protocol-conformance check asserting all six implement the ABC
3. [ ] **Fix the path decoder defect** at `connectors/omp_pi.py:62` — `dir_name.replace("-", "/")` corrupts any hyphenated path segment

---

<a name="adr-003"></a>
## ADR-003: Agent as optional layer, not required stage

**Status:** Accepted · **Date:** 2026-07-30

### Context

The most valuable findings — intent extraction, drift detection, code review, debt identification, next-task ranking — require language understanding. They route through `claude_agent_sdk` → `claude` CLI → 9router → `claude-opus-5`. That chain has four failure points, one of which (`claude` CLI on PATH) is unverified, and it costs money on every unattended run. Meanwhile git evidence, task markers, tool errors, and docs-vs-code deltas are computable deterministically and for free.

### Decision

Two independent analyzers. Deterministic analysis always runs and always produces its complete output. The agent adds a layer on top, consuming deterministic findings as grounding. Agent unavailability, spend-cap exhaustion, or malformed output degrades the run — never fails it. Every finding is tagged `deterministic` or `agent` and displayed distinctly.

### Options Considered

#### Option A: Optional layer (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Medium — two paths to maintain |
| Cost | Bounded by cap; zero when disabled |
| Scalability | Independent |
| Team familiarity | High |

**Pros:** Tool works without any LLM access; the Phase 0 STOP gate becomes survivable rather than project-ending; provenance is legible; costs are optional; deterministic findings are reproducible and testable.
**Cons:** Two code paths. Some findings exist only in one mode.

#### Option B: Agent required for all analysis

**Pros:** One path. Richer output uniformly. Less code.
**Cons:** No `claude` CLI → no product. Network blip → failed run. Spend cap hit → failed run. Ties a local file-analysis tool's availability to a network service. Makes every finding non-reproducible, so nothing can be regression-tested.

#### Option C: Deterministic only

**Pros:** Free, fast, fully reproducible, no external deps.
**Cons:** Cannot extract intent from prose. Cannot detect "I changed my mind at 14:22." Cannot review code. These are the requirements that motivated the project — G2 and G3 in the PRD are unachievable without inference.

### Trade-off Analysis

Option B fails a specific, likely scenario: the PRD's own Phase 0 gate might not pass. `claude_agent_sdk` and `textual` could not be verified from this session, the SDK version in the prior plan predates the `output_format` API, and the correct auth header for 9router is unknown. Betting the entire product on that chain working is an unforced error.

Option C is a genuinely useful tool that fails the actual goals.

The two-path cost of Option A is smaller than it appears because the paths are not alternatives — they compose. Deterministic output feeds the agent as grounding. There is no duplicated logic, only layered logic.

The provenance requirement falls out of this decision and is arguably its most valuable side effect: because there are two analyzers, findings *must* be tagged, which means you can always tell what a model inferred versus what git proved.

### Consequences

**Easier:** Phase 0 failure costs a feature, not the project. Deterministic findings are regression-testable. Costs are controllable. Provenance is structural, not bolted on.

**Harder:** Two paths to maintain and two report shapes. Users see different output depending on agent availability — mitigated by a persistent amber banner when the agent is off.

**Revisit when:** The agent proves reliable and cheap over months, *and* deterministic-only mode has demonstrably no users. Even then, keeping it is cheap insurance.

### Action Items

1. [ ] `findings.provenance` column in migration 002
2. [ ] Agent preflight: check `claude` on PATH, SDK importable, one live call — before any agent work
3. [ ] Amber TUI banner when the agent is disabled, naming the reason
4. [ ] Spend cap halts further calls without discarding completed agent output

---

<a name="adr-004"></a>
## ADR-004: Proposal-only writes, no escape hatch

**Status:** Accepted · **Date:** 2026-07-30

### Context

dreamy runs unattended every 6 hours across every project on the machine. It identifies unfinished work, stale docs, and tech debt — and could plausibly fix some of it. The user's original request said "remediate" and "update all documentation," which reads as write intent.

### Decision

Managed repositories are read-only in every mode. Remediation and documentation updates are emitted as unified diffs and prompts in the output directory. **No `--apply` flag exists.** No TUI action, agent action, or config key can enable repo mutation. Verified by before/after SHA-256 of every touched working tree.

### Options Considered

#### Option A: Proposal-only, no escape hatch (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Cost | A manual review-and-apply step |
| Scalability | Unaffected |
| Blast radius | **Zero** |

**Pros:** An unattended process cannot corrupt work in progress. The invariant is testable with one assertion. No interaction with uncommitted changes, stashes, rebases, or detached heads. Trivially auditable.
**Cons:** You apply changes yourself.

#### Option B: Auto-apply docs in an isolated git worktree

**Pros:** Doc updates land without manual work; the main working tree is untouched; review happens on a branch.
**Cons:** Creating a worktree *is* a repo mutation — it writes `.git/worktrees/`. Worktrees fail on locks, detached HEAD, and mid-rebase states. Fourteen projects × 4 runs/day = 56 worktree operations daily against repos in arbitrary states. The invariant weakens from "never writes" to "never writes except when," and the exception is where bugs live.

#### Option C: Proposal-only + one-click apply from the TUI

**Pros:** Default-safe; explicit confirmation; never unattended.
**Cons:** Requires apply machinery — patch application, conflict handling, rollback — to exist in the codebase. Once it exists, it can be invoked. The strongest form of the guarantee is that the code is not there.

### Trade-off Analysis

This is a blast-radius decision, and the asymmetry is stark. Upside of writing: saved keystrokes on doc updates. Downside: an unattended agent corrupts uncommitted work across every project on the machine, at 04:00, while asleep.

Options B and C both dilute a guarantee that is currently absolute and one-line-testable. "dreamy never writes to your repos" is a claim you can verify by grepping for write calls outside the output directory. "dreamy never writes except worktrees, except confirmed applies" is a claim requiring an audit.

The manual apply step is also not pure cost. Reviewing a proposed doc diff is how you notice the analysis was wrong — a feedback loop auto-apply would remove.

### Consequences

**Easier:** One-assertion invariant. Unattended operation is unambiguously safe. No interaction with any git state. The read-only claim needs no qualification.

**Harder:** Manual apply for every change. High-volume mechanical updates stay manual.

**Revisit when:** Never. If write capability is ever wanted, it belongs in a separate tool with a separate name and separate invocation — not a flag on this one.

### Action Items

1. [ ] Before/after working-tree SHA-256 assertion in the acceptance suite
2. [ ] CI-style grep: no filesystem writes outside the output directory
3. [ ] `⬤ REPO: READ-ONLY` permanently in the TUI footer
4. [ ] Prompt compiler rewrites all evidence paths to absolute output-dir paths (ADR-009)

---

<a name="adr-005"></a>
## ADR-005: launchd `StartInterval` over `StartCalendarInterval`

**Status:** Accepted · **Date:** 2026-07-30

### Context

Reconciliation runs every 6 hours on macOS. launchd offers `StartInterval` (every N seconds since load) and `StartCalendarInterval` (at wall-clock times). The machine is a laptop that sleeps, and a missed run has real cost — sessions age toward the edge of the 30-day window.

### Decision

`StartInterval=21600` with `RunAtLoad=true`.

### Options Considered

#### Option A: `StartInterval=21600` + `RunAtLoad` (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Low — two plist keys |
| Cost | Zero |
| Predictability | Medium — anchored to load time |
| Sleep behavior | Fires on wake if the interval elapsed |

**Pros:** Simple. Reboot-safe via `RunAtLoad`. A missed interval during sleep fires on wake rather than being skipped. Changing cadence is one integer.
**Cons:** Run times drift relative to wall clock; "4am" is not guaranteed.

#### Option B: `StartCalendarInterval` at 00:00/06:00/12:00/18:00

**Pros:** Predictable wall-clock times. Legible in the UI.
**Cons:** launchd runs a missed calendar event at most once on wake, and semantics around sleep are notoriously inconsistent. A laptop closed overnight can miss runs silently. Requires four dict entries; changing cadence means rewriting all of them.

#### Option C: A resident daemon with an internal scheduler

**Pros:** Full control, precise timing, in-process state.
**Cons:** A permanently resident process for a job that runs four times a day. Must handle its own sleep/wake, crash recovery, and restart-on-login — reimplementing launchd, worse.

### Trade-off Analysis

The requirement is "roughly every 6 hours, don't silently skip." It is not "at 04:00 exactly." Nothing downstream depends on wall-clock alignment.

`StartInterval` matches the requirement precisely, and its sleep behavior is the better one: elapsed-interval-fires-on-wake is exactly right for a laptop. The drift it introduces is invisible because the TUI shows the actual next run from `launchctl print` rather than a computed guess.

Option C is a category error — writing a scheduler when the OS ships one.

### Consequences

**Easier:** Two plist keys. Cadence change is one integer plus reinstall. Reboot and sleep are handled by the OS.

**Harder:** Run times drift; a user expecting 04:00 sees 04:37. Mitigated by displaying real times from `launchctl print`, never computing them.

**Revisit when:** A run must align to a wall-clock event (e.g. a report emailed at 9am).

### Action Items

1. [ ] `launchctl bootstrap` + `launchctl print` in acceptance evidence — proves `RunAtLoad` registration without a reboot
2. [ ] TUI reads next-run time live from `launchctl print`, never cached
3. [ ] `install` is idempotent — safe to re-run after an interval change

---

<a name="adr-006"></a>
## ADR-006: Read layer between state and TUI

**Status:** Accepted · **Date:** 2026-07-30

### Context

The TUI renders six views over the state DB. The obvious implementation puts SQL in each widget. A web dashboard was deferred (P2/R17) at the time this decision was taken, but plausible later.

**Vindicated 2026-08-02.** R17 was promoted to P1 and built. The web surface reuses `read.py` unchanged and adds no queries of its own — the second surface this ADR was written to make cheap cost exactly what it predicted. The boundary now reads: **no SQL in `tui/` or `web/`.**

### Decision

`continuity/read.py` exposes typed query functions returning plain dataclasses. The TUI imports only that module. **No SQL appears in `tui/`.**

### Options Considered

#### Option A: Read layer (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Low — one module, ~10 functions |
| Cost | Small indirection |
| Reusability | High |
| Testability | High — testable without a terminal |

**Pros:** Query logic is testable headlessly; a future web surface reuses it rather than forking it; schema changes touch one file; widgets stay presentational.
**Cons:** One more module; a trivial query needs a function.

#### Option B: SQL directly in widgets

**Pros:** Fewer files. Direct.
**Cons:** Query logic becomes untestable without a running TUI. A schema change means grepping every widget. A second surface duplicates every query, and the duplicates drift.

#### Option C: Full repository pattern with ORM

**Pros:** Familiar; strong typing; migration tooling.
**Cons:** An ORM is a pip dependency, violating the core constraint. Heavy machinery for ten read queries.

### Trade-off Analysis

The cost is one module. The benefit is that the R17 web dashboard becomes a rendering exercise rather than a data-layer rewrite, and that every query is testable in a normal test process.

The decisive argument is testability, not future-proofing. Headless `App.run_test()` asserting real data renders in every view (an explicit PRD acceptance criterion) is far simpler when the data functions are independently callable. With SQL embedded in widgets, testing a query means driving a terminal emulator.

Option C fails the dependency constraint for a workload of ten queries.

### Consequences

**Easier:** Headless testing of all query logic. Schema changes localized. R17 becomes cheap. Widgets stay simple.

**Harder:** New views may need new read functions. Ad-hoc exploration goes through the module or a SQLite shell.

**Revisit when:** Never — this is a small, load-bearing boundary.

### Action Items

1. [ ] Create `read.py` before any TUI widget
2. [ ] Lint rule or review check: no `sqlite3` import under `tui/`
3. [ ] Unit-test every read function against a fixture DB

---

<a name="adr-007"></a>
## ADR-007: Project identity on git remote + path, from day one

**Status:** Accepted · **Date:** 2026-07-30

### Context

`projects.id` is currently `stable_id("project", path)` — a hash of the absolute filesystem path. The schema already has an unused `git_remote` column. Cross-machine reconciliation is P2 and may never happen. But identity is the one thing that cannot be retrofitted: change it later and every historical session, episode, finding, and artifact points at a dead ID.

### Decision

Populate `git_remote` on every project from the first run. Keep `path` as the primary identity key for now. Record both, so a future merge has the data it needs.

### Options Considered

#### Option A: Record both, key on path (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Low — one `git remote get-url` per project |
| Cost | One subprocess per project per run |
| Future-proofing | High |
| Migration risk | Low |

**Pros:** Cross-machine merge stays possible; no ID scheme change now; also enables detecting the same repo cloned to two local paths.
**Cons:** A column nothing reads yet.

#### Option B: Path only, add remote later

**Pros:** Marginally simpler now.
**Cons:** Historical rows have no remote. Backfill requires the repos to still exist at their original paths — which is precisely what breaks when you move a project. The data is unrecoverable once lost.

#### Option C: Key identity on remote now

**Pros:** Cross-machine works immediately.
**Cons:** Non-git projects have no remote. Multiple projects share a monorepo remote. Local-only repos have none. Forces a composite key and null-handling complexity for a feature nobody has asked for.

### Trade-off Analysis

This is an asymmetric-cost decision. Recording an unused column costs one subprocess per project per run — negligible. *Not* recording it costs an unrecoverable data gap, because you cannot reconstruct 2026's remotes in 2027 after moving projects around.

Option C over-commits: keying on remote today creates real complexity (monorepos, local-only repos) to serve a hypothetical.

Option A is the cheap option that preserves the expensive option.

### Consequences

**Easier:** Future cross-machine merge is a data problem, not an archaeology problem. Detecting the same repo at two paths becomes possible.

**Harder:** One extra git call per project per run. A column with no reader — which will look like dead code to a future reader, so it needs a comment saying why.

**Revisit when:** A second machine appears, or a project moves and continuity should follow it.

### Action Items

1. [ ] Populate `git_remote` in `resolve.py` via `git remote get-url origin`, empty string when absent
2. [ ] Comment the column in `store.py` explaining it is intentionally unread
3. [ ] Surface "same remote, two paths" as a low-severity finding once ≥2 such pairs exist

---

<a name="adr-008"></a>
## ADR-008: Rename to dreamy; retire `cross-harness-continuity`

**Status:** Accepted · **Date:** 2026-07-30

### Context

The project has carried the descriptive name `cross-harness-continuity` across the skill name, output directory, launchd label, and config paths. The chosen name is **dreamy**. Nothing has shipped, no schedule is installed, and no state directory exists — the rename is free right now and gets progressively more expensive with every artifact created under the old name.

### Decision

Rename all identifiers to `dreamy`. No compatibility shim, no alias, no dual-read.

| Identifier | Before | After |
|---|---|---|
| Skill / CLI | `cross-harness-continuity` | `dreamy` |
| State dir | `~/.local/share/cross-harness-continuity/` | `~/.local/share/dreamy/` |
| launchd label | `com.nick.cross-harness-continuity` | `com.nick.dreamy` |
| Plist | `com.nick.cross-harness-continuity.plist` | `com.nick.dreamy.plist` |
| Python package | `continuity/` | `dreamy/` |
| Per-project dir | `.continuity/` | `.dreamy/` |

The Desktop folder `~/Desktop/dreamer` is cosmetic and renamed manually by the user; nothing in the code depends on it.

### Options Considered

#### Option A: Clean rename, no shim (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Low — nothing has shipped |
| Cost | One sweep of a small codebase |
| Risk | Very low — no installed state to migrate |
| Cleanliness | Total |

**Pros:** One name everywhere. No dead compatibility code. Cheapest it will ever be — no state dir, no plist, no reports exist yet.
**Cons:** Any stale reference in the existing docs must be caught.

#### Option B: Rename with a compatibility alias

**Pros:** Old invocations keep working.
**Cons:** There are no old invocations. Adds permanent dead code to support a user base of zero.

#### Option C: Keep the descriptive name

**Pros:** Self-describing.
**Cons:** Not the chosen name. `cross-harness-continuity` is 25 characters typed into a terminal repeatedly, and its length was already a mild argument against it.

### Trade-off Analysis

The interesting property is timing. Renaming now touches source files and unwritten docs. Renaming after the first scheduled run means migrating a state DB, unloading and reloading a launchd job, rewriting report paths, and fixing every artifact whose footer cites the old output directory.

The window where this is free closes at Phase 5 (schedule install). Doing it now is strictly correct.

### Consequences

**Easier:** One name. Shorter CLI. No migration debt.

**Harder:** Existing spec documents need a sweep for stale references — including this document set, which was written under the old name.

**Revisit when:** Never.

### Action Items

1. [ ] Sweep `SPEC-*.md` and the addendum for `cross-harness-continuity` and `dreamer`
2. [ ] Rename `scripts/continuity/` → `scripts/dreamy/`; update imports in `store.py`, `protocol.py`, `redact.py`, `connectors/omp_pi.py`
3. [ ] Confirm no state dir or plist exists under the old name before proceeding
4. [ ] User renames `~/Desktop/dreamer` → `~/Desktop/dreamy` (manual — the folder is a mount root)

---

<a name="adr-009"></a>
## ADR-009: Inline skill contracts rather than reference them

**Status:** Accepted · **Date:** 2026-07-30

### Context

The prompt compiler builds four artifacts per project from skills in `~/.claude/skills/`. Those artifacts are pasted into whichever harness the work resumes in — including Codex and OpenCode, which cannot resolve Claude-Code-style skill invocations. The entire premise of dreamy is cross-harness portability.

### Decision

Default emit mode is `inline`: read each `SKILL.md` at generation time and inline the relevant constraint blocks as literal text. Artifacts are self-contained. `--harness {claude,omp,pi,codex,opencode,portable}` optionally emits native invocations with inline text as fallback. Every evidence path is rewritten to an absolute path under the output directory.

### Options Considered

#### Option A: Inline by default (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Medium — parse and extract from SKILL.md |
| Cost | Longer artifacts |
| Portability | **Total** |
| Coupling | Reads external files at runtime |

**Pros:** Works pasted into anything, including a plain chat window. Skill improvements propagate to every future artifact for free. No dependency on the target's skill installation.
**Cons:** Longer prompts. Output depends on mutable external files, which conflicts with byte-identical idempotency (N6).

#### Option B: Emit skill references

**Pros:** Short artifacts. Always current.
**Cons:** Inert in Codex and OpenCode — half the harnesses this tool exists to serve. A resumption prompt that silently loses its Iron Rule constraints in Codex is worse than one that never had them, because the failure is invisible.

#### Option C: Vendor skill copies into dreamy

**Pros:** Reproducible; no runtime dependency; N6 preserved exactly.
**Cons:** Copies drift from the originals. You improve `transform-validation-prompt` and dreamy keeps using last month's version, silently. Creates two sources of truth for the same discipline — the exact problem the whole compiler design was meant to avoid.

### Trade-off Analysis

Option B fails the core requirement. Portability is not a nice-to-have here; it is the product.

Option C trades a real, silent correctness failure (stale discipline) for a bounded, visible one (idempotency drift). Stale vendored skills fail quietly and get noticed months later. Idempotency drift shows up immediately in a diff.

Option A's cost — longer artifacts — is irrelevant; these are prompts, not payloads.

The genuine unresolved tension is A's coupling to mutable files versus N6. Recording the skill's content hash in the artifact footer (already planned for R19) makes drift *detectable*, which converts a silent problem into a visible one. Whether to pin on that hash or track latest is **Q11 — still open, and this ADR does not resolve it.**

### Consequences

**Easier:** Artifacts work everywhere. Skill improvements propagate free. No target-side installation required.

**Harder:** Longer artifacts. Byte-identical idempotency is at risk until Q11 is decided. The compiler must parse `SKILL.md` structure, which is a format dreamy does not control — the same fragility class as ADR-002's connectors, and it deserves the same treatment: never raise, warn and degrade.

**Revisit when:** Q11 is decided, or `SKILL.md` conventions change materially.

### Action Items

1. [ ] Compiler reads `~/.claude/skills/<name>/SKILL.md` at generation time; missing skill → bounded warning, degraded-but-valid artifact
2. [ ] Record skill name, path, and content hash in every artifact footer
3. [ ] Grep check: zero unresolved `Skill(`, `/skill:`, `@skill` in default output
4. [ ] Rewrite every `evidence/` path to absolute under the output directory (enforces ADR-004)
5. [ ] **Decide Q11** — pin skill hashes or track latest — before Phase 4
