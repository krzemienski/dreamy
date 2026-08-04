# dreamy — System Design

**Status:** Design review
**Date:** 2026-07-30
**Framework:** requirements → high-level design → deep dive → scale & reliability → trade-offs
**Related:** `SPEC-dreamy.md` (PRD), `ARCHITECTURE.md` (ADRs), `TASK.md` (execution)

> **Naming:** the project is **dreamy**. CLI `dreamy`, skill `dreamy`, state at `~/.local/share/dreamy/`, launchd label `com.nick.dreamy`. The prior `cross-harness-continuity` identifier is retired; ADR-008 covers migration.

---

## 1. Requirements

### 1.1 Functional

| ID | Requirement |
|---|---|
| F1 | Ingest coding sessions from six sources (9router, OMP, Pi, Claude Code, Codex, OpenCode), read-only, incrementally |
| F2 | Attribute every session to a project; correlate the same work across harnesses into one timeline |
| F3 | Extract per work-episode: original intent, completion status, drift, code changes, tech debt, docs drift |
| F4 | Compile four prompt artifacts per project: resumption, validation, remediation, next-tasks |
| F5 | Emit reports (Markdown, JSON, self-contained HTML), centrally and optionally per-project |
| F6 | Run unattended every 6h via launchd, and identically on demand |
| F7 | Present a Textual TUI: runs, findings, projects, prompts, schedule, live monitor |

### 1.2 Non-functional

| ID | Requirement | Target | Why this number |
|---|---|---|---|
| N1 | Incremental run duration | < 2 min (stretch < 45s) | Must finish well inside the 6h window with headroom for a slow agent phase |
| N2 | First (full) run duration | < 10 min | One-time cost; 30 days × 6 sources of cold ingest |
| N3 | Repo mutations | **exactly 0** | Absolute contract. Not a target — an invariant |
| N4 | Secrets persisted | **exactly 0** | Absolute contract |
| N5 | Agent spend per run | ≤ configured cap, ≥20% headroom | Unattended 4×/day; unbounded spend is the top operational risk |
| N6 | Idempotency | Byte-identical stable artifacts on unchanged re-run | Makes diffs meaningful and drift detectable |
| N7 | Availability | Best-effort | Single-user local tool. A missed run costs nothing; the next one catches up |
| N8 | TUI cold open | < 2s to populated view | Above ~2s you stop checking it, and an unchecked dashboard is worthless |

### 1.3 Constraints

- **Single machine, single user.** No auth, no multi-tenancy, no remote-reachable service. The R17 web surface binds `127.0.0.1` and serves GET/HEAD only — a loopback read-only view for the operator already sitting at the machine, not a network service.
- **Python 3.11 stdlib + `git` CLI only** in the core pipeline. Third-party imports confined to the agent module and the TUI. The web surface is stdlib-only (`http.server`) and adds no dependency.
- **macOS.** `launchd`, `fcntl`, `~/Library/LaunchAgents`.
- **Sources are foreign and unversioned.** Six formats owned by six other projects, each free to change without notice. This is the dominant design force — see §3.1.
- **Team of one.** Every subsystem must be independently debuggable by the person who wrote it, six weeks later.

### 1.4 Load estimation

Measured from the real environment where possible; extrapolated where noted.

| Quantity | Value | Source |
|---|---|---|
| 9router `usageHistory` rows (all time) | 234,000+ | Stated in prior plan doc |
| Sessions in a 30-day window | ~500 | Extrapolated from observed activity |
| Turns per session | ~50–200 | JSONL line counts |
| Turn rows in window | ~50,000 | 500 × ~100 |
| Projects | ~15 | Observed |
| State DB size after 30d | ~50–150 MB | Turns dominate; excerpts capped at 200 chars |
| Peak write rate | ~50k rows / full run | Single writer, batched |
| Concurrent readers | 1 (TUI) + 1 (run) | — |

**This is a small-data problem.** 50k rows is three orders of magnitude below where SQLite gets interesting. Any design pressure toward a "real" database, a queue, or a service is unjustified — see ADR-001. The engineering difficulty here is **format fragility and correctness of inference**, not throughput.

---

## 2. High-Level Design

### 2.1 Component diagram

```
┌─────────────────────────── SOURCES (read-only, foreign) ────────────────────────────┐
│  ~/.9router/db/data.sqlite   ~/.omp/agent/sessions/    ~/.pi/agent/sessions/         │
│  ~/.claude/projects/         ~/.codex/sessions/        ~/.local/share/opencode/*.db  │
└────────────────────────────────────────┬────────────────────────────────────────────┘
                                         │  Connector ABC  (discover / scan)
                                         │  → SourceRecord (normalized, redacted)
┌────────────────────────────────────────▼────────────────────────────────────────────┐
│  INGEST                                                                              │
│  · watermark per source        · redact at boundary      · bounded fault handling    │
│  · content-hash stable IDs     · append-only facts                                   │
└────────────────────────────────────────┬────────────────────────────────────────────┘
                                         │
┌────────────────────────────────────────▼────────────────────────────────────────────┐
│  RESOLVE + CORRELATE                                                                 │
│  · project root ← native meta | cwd | git root                                       │
│  · 4-tier confidence match; unmatched retained, never force-joined                   │
└────────────────────────────────────────┬────────────────────────────────────────────┘
                                         │
                    ┌────────────────────┴────────────────────┐
                    │                                         │
┌───────────────────▼──────────────────┐   ┌──────────────────▼─────────────────────┐
│  ANALYZE — DETERMINISTIC             │   │  ANALYZE — AGENT  (optional)           │
│  git status/log/diff · task markers  │   │  claude-agent-sdk → claude CLI →        │
│  tool errors · docs-vs-code touched  │   │  9router → claude-opus-5                │
│  Always runs. Never degrades.        │   │  intent · drift · review · debt · next  │
└───────────────────┬──────────────────┘   │  json_schema structured output          │
                    │                       │  spend-capped · degrades to ⊘ on fail  │
                    └────────────────────┬──┴────────────────────────────────────────┘
                                         │  findings + intent_episodes
┌────────────────────────────────────────▼────────────────────────────────────────────┐
│  PROMPT COMPILER                                                                     │
│  reads ~/.claude/skills/*/SKILL.md → inlines contracts → 4 artifacts per project     │
│  task-architect · transform-validation-prompt · functional-validation ·              │
│  create-validation-plan · goal-condition-architect · Iron Rule trio                  │
└────────────────────────────────────────┬────────────────────────────────────────────┘
                                         │
┌────────────────────────────────────────▼────────────────────────────────────────────┐
│  REPORT              MD · JSON · self-contained HTML · archive · open-item index     │
└────────────────────────────────────────┬────────────────────────────────────────────┘
                                         │
                    ┌────────────────────┴────────────────────┐
                    ▼                                         ▼
      ┌──────────────────────────┐             ┌──────────────────────────┐
      │  read.py  (read layer)   │◀────────────│  TUI (Textual)           │
      │  dataclasses, no SQL out │             │  6 views · cyberpunk     │
      └──────────────────────────┘             └──────────────────────────┘
                    ▲
      ┌─────────────┴────────────┐
      │  launchd  every 21600s   │──▶ same CLI entrypoint as manual
      └──────────────────────────┘
```

### 2.2 Data flow

One direction, no cycles. Each stage reads the previous stage's committed output from SQLite; nothing is held in memory across stages. A crash resumes at the last committed watermark.

```
foreign files ─▶ SourceRecord ─▶ turns/sessions ─▶ projects/links ─▶ episodes/findings ─▶ artifacts ─▶ reports
                 (redacted)      (append-only)     (confidence)      (evidence-cited)     (inlined)   (idempotent)
```

**Invariant:** data only ever moves right. No stage writes back to an earlier stage's tables. This makes the pipeline restartable at any boundary and makes "what produced this finding" answerable by walking left.

### 2.3 Module boundaries

| Module | Owns | Must not know about |
|---|---|---|
| `connectors/*` | One foreign format each | Projects, correlation, findings, anything downstream |
| `protocol.py` | The `Connector` ABC and `SourceRecord` | Any specific source |
| `store.py` | Schema, migrations, watermarks, lock | Semantics of any field |
| `resolve.py` | Project identity, git roots | Harness formats |
| `correlate.py` | Confidence tiers, linking | Harness formats |
| `analyze/deterministic.py` | git + marker evidence | The agent |
| `analyze/agent.py` | SDK calls, schemas, spend cap | Report formatting |
| `compile/*` | Skill inlining, artifact assembly | SQL |
| `report.py` | MD/JSON/HTML rendering | Analysis logic |
| `read.py` | Query → dataclass | Rendering |
| `tui/*` | Widgets, theme, keybindings | **SQL** (per ADR-006) |
| `cli.py` | Argument parsing, orchestration, exit codes | Everything's internals |

The boundary that matters most: **no harness-specific string ever appears above `connectors/`.** If correlation needs to know something is "an OMP session," the abstraction has already failed.

### 2.4 API contracts

No remote API. The R17 web surface exposes a read-only JSON API over loopback
(`GET /api/v1/…`), built entirely on `read.ReadStore` — the same layer the TUI
uses. It is an additional *view*, not an integration boundary: nothing outside
this machine can reach it, and no route mutates state.

```python
# The one contract every source implements. Already built in protocol.py.
class Connector(ABC):
    SOURCE_ID: str
    def default_source_path(self) -> Path: ...
    def discover(self, path_override: str | None = None) -> SourceInfo: ...
    def scan(self, watermark_ms: int, lookback_days: int,
             path_override: str | None = None) -> Iterator[SourceRecord]: ...
```

`scan()` contract, restated because every fault-tolerance property depends on it:
- Missing source → yield nothing, let `discover()` report the gap. **Never raise.**
- Corrupt record → skip it, warn on stderr, continue. **Never raise.**
- The only acceptable exception is programmer error.

```python
# Read layer — the TUI's only door to state (ADR-006)
def latest_run() -> RunSummary
def run_history(limit: int) -> list[RunSummary]
def findings(project: str | None, severity: set[str], state: set[str]) -> list[Finding]
def project_timeline(project_id: str) -> ProjectTimeline
def artifacts(project_id: str, kind: str | None) -> list[Artifact]
def schedule_status() -> ScheduleStatus     # shells launchctl, never cached
```

### 2.5 Storage

| Store | Path | Contents |
|---|---|---|
| State | `~/.local/share/dreamy/state.db` | SQLite, WAL. All tables |
| Config | `~/.local/share/dreamy/config.json` | Intervals, caps, filters, prompt chains |
| Run logs | `~/.local/share/dreamy/runs/{run_id}.jsonl` | Append-only journal, tailed by the monitor |
| Reports | `~/.local/share/dreamy/reports/latest/` | Current run output |
| Archive | `~/.local/share/dreamy/reports/archive/{run_id}/` | Dated snapshots |
| Evidence | `.../reports/latest/projects/{slug}/evidence/` | Validation gate output — **never in a repo** |
| Lock | `~/.local/share/dreamy/lock` | fcntl single-instance |

Nothing is written inside a managed repository in any mode. Optional per-project output is confined to `.continuity/` and is off by default.

---

## 3. Deep Dive

### 3.1 The dominant design force: foreign, unversioned formats

Six formats owned by six projects that owe this tool nothing. OMP can ship a JSONL schema change tomorrow. This single fact drives more of the design than scale, latency, or cost combined.

**Mitigations, in order of importance:**

1. **Isolation.** A format change can only break its own connector. Everything above `SourceRecord` is unaffected.
2. **Never raise.** A connector that throws takes down the run and blocks the other five. The contract forbids it.
3. **Structural warnings, not silence.** `discover()` reports availability; malformed records emit bounded warnings that surface in the run summary and the TUI. Silent degradation is the failure mode that would erode trust fastest — you'd stop noticing a source had gone dark.
4. **Per-run path override.** `--source-path SOURCE=path` makes fault injection deterministic and reproducible, so resilience is tested rather than hoped for.
5. **Watermark isolation.** Per-source watermarks mean one source falling behind doesn't stall the others.

**Explicitly rejected:** schema-version detection and multi-version parsers per connector. That is speculative complexity for a failure that hasn't happened yet. A clear warning plus a fast fix is cheaper than a versioning framework nobody asked for.

### 3.2 Data model

Existing schema in `store.py` (`SCHEMA_V1`) plus additions this design requires.

```
projects ──┬── sessions ──── turns
           │      ▲
           │      └──── router_requests.linked_session_id  (nullable, confidence-scored)
           ├── intent_episodes
           ├── findings
           └── prompt_artifacts

watermark (per source)      runs (journal)      schema_version
```

**Additions required beyond `SCHEMA_V1`:**

| Table/column | Purpose | Requirement |
|---|---|---|
| `findings.dismissal_reason` | wrong / wont-fix / already-done / duplicate | R20 |
| `findings.state` | new / persisting / resolved / regressed | R13 |
| `findings.provenance` | `deterministic` \| `agent` | TUI (D)/(A) marker |
| `prompt_artifacts.chain_json` | skills + hashes used to compile | R19 provenance |
| `prompt_artifacts.health_json` | cold-start, citations, sections, evidence-dir checks | TUI health panel |
| `runs.lookback_days` | window the run used | R11 — a report must state its own window |
| `sessions.platform` | ios/cli/api/web/fullstack/generic | Cached per project, drives R6b |

**Key design choices:**

*Stable IDs are content hashes.* `stable_id("turn", session_id, ts, role, tool, fingerprint)`. Re-ingesting is a no-op via `INSERT OR IGNORE`. This is what makes idempotency (N6) and safe re-runs at a longer lookback (R11) fall out for free rather than needing dedup logic.

*Append-only facts, derived analysis.* `sessions` / `turns` / `router_requests` are never updated after insert. `intent_episodes` / `findings` are recomputed. Re-analysis never risks corrupting the evidence it reasons from.

*Content is fingerprinted, not stored.* `turns.content_fingerprint` holds a hash; only a redacted 200-char excerpt survives. Keeps the DB small and shrinks the secret-leak surface to a bounded, auditable field.

*`git_remote` populated from day one.* Nothing reads it yet. ADR-007 explains why it goes in now anyway.

### 3.3 Correlation

Four tiers, tried in order, first match wins.

| Tier | Rule | Confidence | Notes |
|---|---|---|---|
| T1 | Exact native/request/session key | `exact` | Only when a source records the other's ID |
| T2 | Same project + timestamp within ±N s + same model | `high` | N default 30s, configurable |
| T3 | Normalized prompt fingerprint + project + bounded window | `medium` | Fingerprint over redacted, whitespace-normalized excerpt |
| T4 | No match | `unlinked` | **Retained in full** |

**Design stance:** a wrong link is worse than no link. A false correlation merges two unrelated work episodes and produces a resumption prompt describing work that never happened — the single most damaging failure this system can produce, because it is confidently wrong. T4 is a first-class, permanent outcome, displayed in reports, not a bucket to be minimized.

Every link stores tier, confidence, and a human-readable reason. Router linkage is enriching only: a session with no router match is analyzed identically.

### 3.4 Deterministic vs agent analysis

Two independent analyzers over the same evidence.

| | Deterministic | Agent |
|---|---|---|
| Inputs | git status/log/diff, task markers, tool errors, docs-vs-code file sets | Redacted transcript excerpts + deterministic findings |
| Produces | file changes, docs drift, error episodes, unresolved failures | intent, completion judgment, drift, code review, debt, next tasks |
| Reliability | Reproducible | Non-deterministic, schema-validated |
| Failure mode | Bug (fix it) | Unavailable / capped / malformed (degrade) |
| Marked as | `(D)` | `(A)` |

**The relationship is layering, not fallback.** Deterministic analysis always runs and always produces its full output. The agent adds a layer on top. If the agent is unavailable, disabled, or over budget, the run still completes and still produces reports — with fewer findings and an amber banner, never an error.

This is the resilience property that makes the STOP gate in the PRD survivable: if 9router routing can't be made to work, dreamy is still a working tool.

*Why the agent sees deterministic findings as input:* it grounds inference in facts and lets the agent explain *why* something is incomplete rather than re-deriving *that* it is.

### 3.5 Agent integration

```
analyze/agent.py
   └─ claude_agent_sdk.query(options=ClaudeAgentOptions(
        env={"ANTHROPIC_BASE_URL": "http://localhost:20128",
             "<AUTH_VAR>": <read at call time from env or keychain>},
        model="claude-opus-5",
        output_format={"type": "json_schema", "schema": EXTRACTION_SCHEMA},
      ))
        └─ spawns `claude` CLI subprocess   ◀── HARD DEPENDENCY
             └─ HTTP → 9router :20128
                  └─ provider → claude-opus-5
                       └─ writes a usageHistory row  ◀── our proof of routing
```

Facts from the SDK source that shape this:

- **The SDK does not read `ANTHROPIC_BASE_URL`.** It spawns the `claude` CLI and merges `options.env` into that subprocess environment. The `claude` binary is therefore a hard runtime dependency — a fact absent from the prior plan.
- **Structured output is `output_format={"type":"json_schema", ...}`**, read from `ResultMessage.structured_output`. Validation happens at the tool-call layer, so the model retries on mismatch. Do not prompt-and-parse.
- **`ANTHROPIC_AUTH_TOKEN` sends `Authorization: Bearer`; `ANTHROPIC_API_KEY` sends `x-api-key`.** Which one 9router accepts is a gate output, not an assumption.

**Spend cap enforcement (N5):** cost is estimated per call before dispatch and accumulated after. Crossing the cap halts *further* agent calls and marks remaining projects `agent_skipped` — it never aborts the run or discards completed agent output. Degradation is graceful and partial by design.

**Auth handling:** read from env or keychain at call time, held in a local, never written to config, state, logs, evidence, or TUI state. The canary grep in the acceptance suite covers auth tokens as well as source secrets.

### 3.6 Prompt compilation

Detailed in the PRD addendum Part A. Three properties that belong in the system design:

**Skills are read at generation time, not vendored.** The compiler reads `~/.claude/skills/<name>/SKILL.md` on every run. Improving a skill improves every subsequent prompt with no code change. Cost: output depends on mutable external files — which is exactly the tension in open question Q11 (pin vs. track latest), and it is a real conflict with the byte-identical idempotency requirement N6.

**Output is self-contained by default.** Skill references are inlined as literal text, because generated prompts get pasted into Codex and OpenCode where `Skill(...)` is inert. A prompt that only works in half your harnesses defeats the premise of a cross-harness tool.

**Evidence paths are rewritten to absolute.** `transform-validation-prompt` emits `evidence/…` relative to cwd — i.e. inside the repo. Every such path is rewritten under the output directory before emission, or the read-only invariant (N3) is violated by the artifacts themselves.

### 3.7 Concurrency and locking

Single writer, enforced by `fcntl.flock` on `~/.local/share/dreamy/lock`. A second run exits immediately with a clear message and nonzero status — it does not queue, retry, or wait.

*Why not queue:* runs are idempotent and scheduled every 6h. A run that collides with a long-running predecessor has nothing useful to add; the next scheduled run picks up everything. Queueing would add a state machine to solve a problem that resolves itself in 6 hours.

The TUI is a **reader** and takes no lock. WAL mode permits concurrent reads during a run — which is what makes the live monitor view possible at all.

### 3.8 Scheduling

```xml
<key>StartInterval</key><integer>21600</integer>
<key>RunAtLoad</key><true/>
```

`StartInterval` over `StartCalendarInterval` — see ADR-005. `RunAtLoad` gives reboot safety and means a laptop that was asleep at 04:00 reconciles on wake rather than silently skipping.

Manual and scheduled execution invoke the identical CLI path. There is no "scheduled mode." A parity test asserts equivalent artifacts, because divergence between the two would produce bugs that only appear unattended — the worst kind to debug.

The TUI reads schedule state by shelling `launchctl print` live, never from cache. A cached "next run" that has silently drifted from reality is worse than no display at all.

---

## 4. Scale and Reliability

### 4.1 Scaling

**Vertical, and it does not matter.** 50k rows, one user, one machine. Sequential SQLite writes are ~4 orders of magnitude faster than needed.

Where time actually goes in a full run:

| Stage | Est. share | Bound by |
|---|---|---|
| Ingest | 15% | File I/O, JSONL parse |
| Correlate | 5% | In-memory, indexed |
| Deterministic analyze | 20% | `git` subprocess spawns — one per project |
| **Agent analyze** | **55%** | **Network round-trips to 9router** |
| Compile + report | 5% | String assembly |

**The agent stage is the only thing worth optimizing, and only when measured.** Deterministic analysis parallelizes trivially per project if git spawns become a problem. Agent calls parallelize across projects bounded by the spend cap.

Growth to 100 projects or 5 years of history changes nothing structurally: watermarks keep incremental runs proportional to *new* data, not total data.

### 4.2 Failure modes

| Failure | Detection | Behavior | Severity |
|---|---|---|---|
| Source path missing | `discover()` | Warn, skip source, continue | Low — expected |
| Malformed record | JSON/SQL parse | Skip record, warn with citation, continue | Low |
| Source DB locked | `sqlite3.OperationalError` | Retry ×3 backoff, then warn and skip | Low |
| `git` absent from PATH | Startup check | **Exit nonzero with clear message** | **Fatal** — hard dep |
| `claude` CLI absent | Agent preflight | Disable agent, amber banner, run continues | Medium |
| 9router unreachable | SDK error | Disable agent for the run, warn, continue | Medium |
| Agent schema mismatch | `output_format` validation | Retry once, then bounded warning | Low |
| Spend cap reached | Accumulator | Halt further agent calls, mark `agent_skipped` | Low — by design |
| Concurrent run | flock | Exit nonzero immediately | Low |
| Interrupted run | Watermark on restart | Resume from last commit; no duplicates (content-hash IDs) | Low |
| State DB corrupt | `PRAGMA integrity_check` on open | **Refuse to run**, instruct rebuild | High — never write to a corrupt DB |
| Disk full | `OSError` on write | Abort with clear message, leave DB consistent (WAL) | Medium |
| Skill file missing | Compiler resolve | Warn, degraded-but-valid artifact | Low |
| Broken prompt citation | Cold-start check | Mark artifact `health: FAIL`, still emit | Medium — visible, not hidden |

**The governing principle:** exactly two conditions are fatal — missing `git` (a hard evidence dependency) and a corrupt state DB (writing to it would compound the damage). Everything else degrades with a visible warning. An unattended job that aborts on a missing optional source is a job you stop trusting.

### 4.3 Monitoring and alerting

No external monitoring — there is no service to page anyone about.

**Self-observability:**
- `runs` table: status, durations, per-source counts, warning/error counts, lookback
- `runs/{run_id}.jsonl`: append-only stage journal, tailed live by the TUI monitor
- TUI Runs view: last run, sources with 30-day sparklines, attention panel
- `dreamy status`: machine-readable health for scripting

**The alert is the dashboard.** A source that goes dark shows an empty sparkline and a stale count. Three consecutive failed runs show as red rows in history. This is appropriate for a single-user local tool — a notification system would be infrastructure serving nobody.

### 4.4 Data retention

| Data | Retention | Rationale |
|---|---|---|
| `turns` | Rolling 90 days | Dominates DB size; older turns have no analytical value |
| `sessions`, `projects` | Indefinite | Small, and needed for long-range recurring-goal detection |
| `findings`, `intent_episodes` | Indefinite | The historical record is the point |
| `prompt_artifacts` | Latest + 10 archived per project per type | Enough to diff; not enough to bloat |
| `runs/*.jsonl` | 30 days | Debugging aid, not a record |
| `reports/archive/` | 90 days | Configurable |

Retention is enforced at the end of each run, inside the same lock.

---

## 5. Trade-off Analysis

### 5.1 Decisions and their costs

| Decision | Chosen | Rejected | We gain | We pay | Revisit when |
|---|---|---|---|---|---|
| Storage | SQLite | Postgres, DuckDB, files | Zero-install, stdlib, WAL, atomic | Single-writer | Never, realistically |
| Language | Py 3.11 stdlib | Rust, Go, TS | No build, no deps, fast iteration | Slower ingest | Full run > 10 min |
| Cadence | Batch 6h | Live watching | Simple, no daemon, no FS watchers | Up to 6h stale | You want same-hour feedback |
| Write posture | Proposal-only | Auto-apply, worktree | Zero blast radius, unattended-safe | Manual apply step | Never — this is the contract |
| Agent | Optional layer | Required, or none | Degrades gracefully; deep analysis when available | Two analysis paths | — |
| Correlation | Confidence tiers | Force-match, exact-only | Honest links, no false merges | Some real links missed | FP rate observed high |
| Prompt skills | Read at runtime | Vendored copies | Skill fixes propagate free | Output depends on mutable files | Q11 — conflicts with N6 |
| UI | Textual TUI | Web, both | Terminal-native, SSH-able, matches plan | Limited color fidelity | R17, if wanted |
| Scheduler | launchd | cron, daemon | OS-native, reboot-safe, no process | macOS-only | Cross-platform needed |
| Locking | fcntl, fail fast | Queue, retry | Trivial; idempotent runs make it safe | Collided run is skipped | Runs approach 6h |

### 5.2 The three real tensions

**T1 — Runtime skill reads vs. byte-identical idempotency (N6).**
Reading `SKILL.md` at generation time means a skill edit changes tomorrow's output, breaking byte-identical repeat runs. Options: pin a hash per artifact and warn on drift (preserves N6, requires an explicit refresh); track latest (loses N6 for artifacts, keeps improvements automatic); or scope N6 to reports only and exempt artifacts. **Unresolved — Q11.** The pinning option is probably right, since a resumption prompt that silently changes between runs undermines the diff view.

**T2 — Agent depth vs. unattended spend.**
Deeper analysis costs more, and 4 runs/day compounds. The cap is a hard ceiling but a blunt one — it truncates by project order, not by value. A smarter policy would spend on projects with recent activity and skip dormant ones. **Deferred:** ship the blunt cap, measure real cost, then refine. Optimizing spend before observing it is guesswork.

**T3 — Inference richness vs. trust.**
The agent produces the most valuable findings (intent, drift, review) and is the only source of wrong ones. Every mitigation — `(A)` provenance marking, evidence citations, confidence scores, `unverified` over `complete`, dismissal tracking — exists to keep model inference legible as inference. **The failure to avoid is not "the agent is sometimes wrong." It is "you can't tell which findings came from a model."**

### 5.3 What to revisit as this grows

| Trigger | Revisit |
|---|---|
| Full run > 10 min | Parallelize git spawns; consider a faster ingest path |
| Agent spend > 50% of cap routinely | Value-based spend policy (T2) |
| Findings dismissal rate > 20% in a category | That analyzer is miscalibrated — retune or cut it |
| A second machine appears | ADR-007 identity model activates; design a merge |
| Sessions > 5,000 in window | Partition `turns`; add covering indexes |
| Wanting to share reports | R14 bundle export, then R17 web surface via `read.py` |
| A harness changes format | Connector-local fix. If it happens 3+ times, reconsider §3.1's rejection of version detection |

---

## 6. Assumptions

Recorded explicitly; several are unverified from this session because the sandbox reaches only the project folder, not the macOS home directory.

| # | Assumption | Basis | If wrong |
|---|---|---|---|
| A1 | 9router at `localhost:20128`, Anthropic-compatible | Prior plan doc | Agent layer is disabled; core unaffected |
| A2 | `claude` CLI installed and on PATH | Required by SDK subprocess model | Agent layer disabled — **unverified** |
| A3 | `claude_agent_sdk` present at a version supporting `output_format` | Prior doc says 0.1.68; that predates the API | Prompt-and-parse fallback, or agent off — **unverified** |
| A4 | `textual` installed | Prior doc says 8.1.1 | TUI skipped with documented gap — **unverified** |
| A5 | 9router accepts `claude-opus-5` as a model id | Assumed | Needs a route alias — `.omp/config.yml` shows `zai/glm-5.2`, so naming is non-obvious |
| A6 | Source paths match the documented defaults | Prior doc | Per-source override exists for exactly this |
| A7 | Sessions record cwd or a decodable project path | OMP/Claude directory encoding observed | Attribution degrades to fuzzy tier |
| A8 | ~500 sessions / 30 days | Extrapolated | Only affects sizing, not design |

A2–A4 are the Phase 0 inventory gate. **Nothing in the agent or TUI layer should be built before they are checked.**
