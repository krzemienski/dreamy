# OBSERVABILITY

Requirements: `OBS-001`, `OBS-002`, `OBS-003`, `OBS-004`, `OBS-005`

Describes what Dreamy emits, stores, correlates, and displays about its own
runs — as it exists at `phase/00-foundation`, and separately what the brief
requires that does not exist yet. Every claim below was checked against
`src/dreamy/logging_util.py`, `store.py`, `evidence_projection.py`,
`redact.py`, and `tui/views/*.py`. Sections marked **SPECIFIED** describe
required behaviour that is not built; none may be cited as evidence for an
`OBS-*` requirement until it exists and is tested. This mirrors the
implemented/SPECIFIED split used in `AGENT-PIPELINE.md` and
`SDK-CAPABILITY-MATRIX.md`.

## Implementation status

| Surface | Status |
|---|---|
| Redacted JSONL event stream (topic files + per-run file) | **implemented** — `logging_util.py` |
| In-memory 500-event ring for live display | **implemented** — `logging_util.py` |
| `agent_calls` / `agent_events` SQLite tables | **implemented** — `store.py` schema v2 |
| Correlation by run id | **implemented** — every event carries `run_id` when a run is active |
| Correlation by trace/parent id (SDK subagent hierarchy) | **NOT implemented** — no field exists |
| Live monitor view (Textual) | **implemented but unexercised** — `textual` not installed in this environment; see `SDK-VERSION-LOCK.md` § "Consequence of the Textual gap" |
| Historical orchestration view (24h replay) | **implemented but unexercised**, same gap |
| Permission-decision events | **SPECIFIED** — no hook wiring exists (`AGENT-PIPELINE.md` § Hooks is SPECIFIED) |
| Retry events | **not applicable to current code** — `agent_sdk.call_claude` makes exactly one paid attempt and does not retry (see § "No retries" below); a retry event schema is SPECIFIED for when retry logic is added |
| Todo/human-input-wait events | **SPECIFIED** — no such SDK surface exists; `OBS-004`'s "todos" and "human-input waits" have no source in `0.2.128` (`SDK-CAPABILITY-MATRIX.md` row 15 — "not-applicable", no `Todo*` export) |
| Structured cost/session-lifecycle events | **partially implemented** — cost is persisted per agent call (`agent_calls.cost_usd`); session-level start/end derivation from the message stream is **SPECIFIED**, not built |

## What exists today

### The event stream (`OBS-001` — partial)

`logging_util.py` is the sole event-writing surface. Every `TopicLogger`
instance (`get_logger(topic)`) is bound to one fixed topic string at
construction; calling `.info()`/`.warn()`/`.error()`/`.debug()` on it always
writes under that bound topic, **regardless of any `agent_type=` keyword
passed as a field**. This matters concretely: `run.py:649` constructs one
`AgentContext` with `log=logging_util.get_logger("research")` and reuses it
across research, teacher, friends, and compiler agent calls in the same
project loop (`run.py:641-735`). Each of those calls passes
`agent_type="teacher"` / `"friends"` / `"compiler"` as an event *field*, but
the physical write target — `logs/research.jsonl` and (if a run is active)
`runs/<run_id>.jsonl` — is fixed to `"research"` for all four. Every event's
`topic` field on disk therefore reads `"research"` even when its
`agent_type` field says `"compiler"`. `agent_type` is the correlation field
that must be trusted for per-agent breakdowns, not the topic-derived file
name.

`TOPICS` (`logging_util.py:13-25`) enumerates eleven topic strings —
`ingest, correlate, analyze, agent, research, teacher, friends, compiler,
report, schedule, system` — but the tuple is declarative only. Nothing in
the codebase validates a topic against it or filters logging by it.
`Config.log_topics` (`config.py:48-53`) is populated with a similar list at
config-load time but has zero read sites anywhere in `src/dreamy/` — it is
dead configuration today, not a functioning filter.

Every write path (`_write()`, `logging_util.py:54-85`):

1. Redacts `msg` via `redact.redact()` if it is a string, and redacts every
   field value via `redact.redact_dict()` (`logging_util.py:50-51, 67-68`).
2. Appends the event to a 500-entry in-memory ring (`_RING_MAX = 500`),
   evicting the oldest on overflow.
3. Appends one JSON line to `logs/<topic>.jsonl`.
4. If a `run_id` was set via `configure_logging()`, appends the **same**
   line to `runs/<run_id>.jsonl`.

Log level filtering is global and set once at `configure_logging()` time
(`cfg.log_level`, default `"DEBUG"`); there is no per-topic level override.

### Storage (`OBS-001`, `OBS-003` — partial)

Two storage surfaces, not one:

- **JSONL topic/run files** under `<output_dir>/logs/*.jsonl` and
  `<output_dir>/runs/<run_id>.jsonl` — the append-only event stream above.
  Purged by age via `retention.py:purge_old_artifacts()`, called from
  `run.py:866-872` when `cfg.retention_days > 0` (default 90). Purging
  tombstones a directory with a `.retention-removed` marker rather than
  silently vanishing it (`retention.py:34-46`).
- **`agent_calls` / `agent_events` SQLite tables** (`store.py` schema v2,
  `store.py:153-182`) — structured, queryable rows, distinct from the JSONL
  stream. `agent_calls` is one row per paid SDK invocation (`insert_agent_call`
  / `finish_agent_call`, `store.py:477-498`): `run_id`, `agent_type`, `model`,
  `started_ms`, `ended_ms`, `status`, `cost_usd`, `prompt_tokens`,
  `completion_tokens`, `input_fingerprint`, `output_json`, `error_text`.
  `agent_events` (`insert_agent_event`, `store.py:500-507`) is a free-form
  event row keyed by `run_id`/`agent_type`/`call_id` with `topic`, `level`,
  `message`, `fields_json` — used today only by `teacher.py:133-145` to
  persist per-explanation cache entries, and read back by
  `teacher.py:56-64` (`_lookup_cached`) as a lookup cache, not as a
  general-purpose event query surface. Neither table is redacted at the
  `store.py` layer — `store.py` imports no redaction module at all
  (confirmed: `fcntl, hashlib, os, re, sqlite3, time, datetime, Path` are its
  only imports). Redaction happens **before** the call reaches `store.py`,
  at the caller: `correlate.py` redacts `error_text`, `file_paths_json`,
  and `raw_meta_json` before calling `store.insert_turn()`
  (`correlate.py:670-672`, importing `redact, redact_value` from
  `redact.py`), and `agent_sdk.py`'s `call_claude()` writes `error_text` and
  `output_json` into `agent_calls` **unredacted** on both the exception path
  (`agent_sdk.py:274-283`, `error_text=f"{type(exc).__name__}: {exc}"`) and
  the success path (`agent_sdk.py:328-338`, `output_json=json.dumps(...)`).
  This is a real gap: an exception message from a paid SDK call could in
  principle contain interpolated user data, and it is persisted to
  `state.db` without passing through `redact.redact()`. It is not covered by
  the `SAFE-002` claim in `AGENT-PIPELINE.md` § "The redaction gap", which
  discusses only the SDK's own hook surface, not this call site.

The 500-event in-memory ring is never persisted; it exists purely to back
live TUI reads without re-parsing JSONL on every frame, and is lost on
process restart.

### Correlation identifiers (`OBS-002` — partial)

What is actually threaded through an event today:

| Field | Source | Coverage |
|---|---|---|
| `run_id` | `configure_logging(run_id=...)`, set once per `run_pipeline()` invocation (`run.py:350-352`) | Present on every JSONL event once a run starts; `None`/absent before that |
| `topic` | Fixed at `TopicLogger` construction | See caveat above — not reliable as an agent-identity field when a context object is reused |
| `agent_type` | Passed explicitly as a keyword field on each `.info()`/`.warn()` call (e.g. `agent_type="compiler"`, `run.py:713-720`, `compiler.py:94-101`) | Present wherever a call site chose to pass it; not enforced by the logger |
| `project_id` | Passed explicitly, same mechanism | Present on most agent-lifecycle events; absent on ingest/correlate/analyze-phase events, which precede project resolution |
| `call_id` | Generated by `store.insert_agent_call()` (`store.py:481`, a `stable_id("agent_call", run_id, agent_type, started_ms, input_fingerprint)` hash) | Exists **only** in the `agent_calls` SQLite table, not in the JSONL stream — `call_claude()` never logs via `logging_util`, it only writes to `store` |
| `connection_id` | Router-side identifier (`router_requests.connection_id`), used by `correlate.py`'s tiered session-linking (`_is_id_shaped_key`, `_id_shaped_values`) | Correlates a router request to a session, not an event to an agent call |
| trace / span / parent identifier | — | **does not exist anywhere in the codebase.** Confirmed by grep across `src/`: zero occurrences of `trace_id`, `span_id`, or `parent_id`/`parent_call_id` |

`OBS-002` requires correlation "by run, trace, agent, and parent
identifiers." Run and agent correlation exist in the partial, inconsistent
form described above. Trace and parent identifiers — which would let an
event be attributed to a specific subagent invocation nested under a
specific parent call, per the SDK's own `SubagentStop` payload
(`agent_id`, `agent_transcript_path`, `agent_type` — confirmed present in
`SDK-VERSION-LOCK.md` § "Hook events actually accepted") — are entirely
absent. `call_id` from `store.agent_calls` is the closest existing
analogue to a span identifier, but it lives only in SQLite and is never
attached to a JSONL event, so it cannot today be used to correlate a log
line back to the paid call that produced it.

### Timelines (`OBS-003` — partial)

Two distinct timeline surfaces exist:

- **Live**: `MonitorView` (`tui/views/monitor.py`) reads the last 24h of
  events across ten topics via `ReadStore.topic_events()`
  (`read.py:402-414`), which globs every `logs/*.jsonl` file, filters by
  `since_ms` and an optional topic allowlist, sorts newest-first, and caps
  at `limit` (default 500). It renders a fixed six-stage pipeline view
  (`ingest → correlate → analyze → agent → compiler → report`,
  `monitor.py:16`) plus a scrolling `time / topic / level / message` table.
  The `[f] follow` binding shown in the monitor's own status line
  (`monitor.py:71`) has **no corresponding action** in `DreamyApp.BINDINGS`
  (`tui/app.py:208-233`) — `f` is bound globally to `action_filter`
  (findings filtering), not a monitor auto-refresh. The monitor view has no
  `set_interval` or other polling call anywhere in `app.py`; it renders
  once on mount and does not update itself. "Live" today means "populated
  from the last 24h of on-disk logs at the moment the tab is opened," not
  streaming. This is a real gap between the button label shown to the
  operator and the wired behaviour.
- **Historical**: `RunsView` (`tui/views/runs.py`) reads
  `ReadStore.runs_history(limit=20)` — one row per `runs` table entry
  (started, status, duration, findings, prompts, cost) — and `ProjectsView`
  (`tui/views/projects.py`) reads per-project `intent_episodes` with
  completion glyphs and drift markers via `project_detail()`.

Both are read-only views over `ReadStore`; per `ADR-006` (referenced in
`read.py:1-4` and enforced by convention across every `tui/views/*.py`
file, none of which import `sqlite3` or `store`), no SQL executes inside
widget code — every value arrives as a plain dataclass.

### Metrics (`OBS-003`, `OBS-004` — partial)

`cost.py` computes `cost_per_project()` and `cost_summary()` — total,
median, p90 spend per project over a 30-day window, sourced from
`ReadStore.cost_30d()` which joins `router_requests` to `sessions` on
`linked_session_id` (`read.py:369-398`). `ReadStore.total_agent_spend()`
(`read.py:206-210`) sums `agent_calls.cost_usd` with no time bound, and
backs the header `AgentSpend` widget refreshed on mount and after every
manual run (`app.py:161-165, 251`). No streaming or subscription-based
metric update exists; every metric is a point-in-time SQL aggregate read
on demand.

### Orchestration views (`OBS-003` — partial, environment-blocked)

Six Textual views exist as source (`RunsView`, `FindingsView`,
`ProjectsView`, `PromptsView`, `ScheduleView`, `MonitorView`), wired into
`DreamyApp` (`app.py:116-136`) with numbered tab bindings `1`-`6`. None of
this can be exercised in the current environment: `textual` is not
installed, `dreamy.tui.*` is unimportable (confirmed by
`SDK-VERSION-LOCK.md` § "Consequence of the Textual gap" and the ground
truth supplied to this document). The source exists and was read by AST,
not by running it. G11 in `GATE-MATRIX.md` records this as `BLOCKED` for
exactly this reason — no TUI behaviour has been observed in this
environment, and none is asserted here.

## What does not exist (SPECIFIED)

### Session-lifecycle events — SPECIFIED

Python has no callback hook for session start or end
(`SDK-CAPABILITY-MATRIX.md` § "Hook coverage vs. the product brief",
confirmed by `SDK-VERSION-LOCK.md`'s authoritative hook-event list, which
contains no `SessionStart`/`SessionEnd`). Session boundaries must be
**derived from the message stream**: the first message received from
`query()` marks session start; the final `ResultMessage` — the message
carrying `total_cost_usd`, `usage`, `model_usage`, `stop_reason`,
`structured_output` — marks session end. No code derives this today.
`call_claude()`'s single-attempt structure (`agent_sdk.py:268-363`) makes
exactly one `_run_query_sync()` call and reads its return tuple directly;
it does not iterate the message stream to observe intermediate messages,
because `_collect_query()` (`agent_sdk.py:417-442`) already consumes the
`async for msg in query(...)` loop internally and returns only the final
aggregated values. A session-lifecycle event stream would require exposing
per-message observations from inside `_collect_query()` — a structural
change, not an additive one.

### Cost-update events — SPECIFIED, by design not a hook

Cost is never a hook event, in the SDK or anywhere Dreamy could add one.
It must be read from `ResultMessage.total_cost_usd`, `.usage`, and
`.model_usage` after the call completes — which is exactly what
`_collect_query()` does today (`agent_sdk.py:429-442`, reading `sr =
getattr(msg, "usage", None)` and `getattr(msg, "total_cost_usd", 0.0)` off
the final `ResultMessage`). What is **implemented**: the resulting
`cost_usd` float is persisted to `agent_calls.cost_usd` at call completion
(`agent_sdk.py:328-338`). What is **SPECIFIED**: an incremental cost-update
*event* emitted mid-call, which cannot exist because there is no
mid-call cost signal to emit it from — the SDK reports total cost only at
`ResultMessage` time, not incrementally per tool call or per token.

### Agent-completion events, correctly attributed — SPECIFIED

`SubagentStop` is a real, documented hook that carries `agent_id`,
`agent_transcript_path`, and `agent_type` (confirmed by both the ground
truth supplied to this document and `SDK-VERSION-LOCK.md`'s authoritative
hook list, which includes `SubagentStop`). Nothing in `agent_sdk.py`
registers a `hooks=` argument on `ClaudeAgentOptions` — confirmed by
reading the full `options = ClaudeAgentOptions(...)` construction
(`agent_sdk.py:241-254`), which sets `model`, `env`, `cwd`,
`max_budget_usd`, `cli_path`, and conditionally `output_format` and
`system_prompt`, and nothing else. There is no subagent dispatch in
Dreamy's code today at all — `AGENT-PIPELINE.md` § "Two execution modes"
confirms `query()` is the only mode in use; `ClaudeSDKClient`, which
subagents would require, is SPECIFIED, not implemented. `SubagentStop`
therefore has nothing to fire on yet. When subagents are added, this hook
is the correct correlation source for `agent_id` → `parent` attribution —
which is also the missing piece for `OBS-002`'s trace/parent requirement.

### Redaction before persistence — SPECIFIED (structurally, not a hook)

Confirmed by `SDK-CAPABILITY-MATRIX.md` § "Hook coverage vs. the product
brief" and repeated verbatim in `AGENT-PIPELINE.md` § "The redaction gap":
no documented SDK hook fires before local transcript persistence.
`PostToolUse.updatedToolOutput` changes only what the model sees, not
what is written to disk, and by the time `PostToolUse` fires any real
side effect has already happened. This means Dreamy's own boundary is the
only place redaction can occur, and it is a structural claim, not a
feature to add — which is why `redact.py` is deliberately built with zero
internal dependencies (confirmed: its only imports are `os, re, pathlib,
typing`), so nothing in a higher layer can accidentally route data around
it. `redact.py`'s own comment records a real prior incident (`redact.py:30-49`):
a 64-character bare-hex CLI secret reached `state.db` and `state.db-wal`
undetected because every earlier pattern was anchored on a literal prefix
(`sk-`, `Bearer`, `tok_`, `AKIA`, `gh?_`) that a bare-hex token never
carries. The fix was known-value redaction — read the live credential from
its known location (`~/.9router/auth/cli-secret` or an env var in
`_SECRET_ENV_VARS`) and strip it by exact occurrence, since no regex broad
enough to catch 64-char lowercase hex can avoid also shredding legitimate
SHA-256 content that this pipeline stores everywhere (content
fingerprints, `stable_hash` values, manifests). The **current gap**
documented above — `agent_sdk.py`'s unredacted `error_text`/`output_json`
writes to `agent_calls` — is exactly the kind of boundary this incident
warns about: a write path that reaches `store.py` without first passing
through `redact.py`.

### Permission-decision, tool, hook, and retry events — SPECIFIED

`OBS-004` requires permission decisions, tools, hooks, retries, costs,
todos, and human-input waits all be observable. Of these seven, only costs
are implemented (as an end-of-call value, not a stream — see above). The
rest require SDK surfaces that are themselves SPECIFIED, not implemented,
per `AGENT-PIPELINE.md`:

- **Permission decisions** — the permission-evaluation order (`Hooks →
  Deny rules → Ask rules → Permission mode → Allow rules → can_use_tool`)
  is documented and will be configured (`AGENT-PIPELINE.md` § Permissions),
  but no `permission_mode`, `can_use_tool`, or hook is set in `agent_sdk.py`
  today, so there is nothing to observe yet.
- **Tools** — Dreamy's eight narrow read-only MCP tools
  (`dreamy_read_session_excerpt`, etc.) are SPECIFIED, not built.
- **Hooks** — no `hooks=` argument is passed anywhere; the ten accepted
  hook events are enumerated in `SDK-VERSION-LOCK.md` as *available*, not
  as *used*.
- **Retries — no retries.** `call_claude()`'s inline comment is explicit:
  *"No silent retry — the model could return the same shape."*
  (`agent_sdk.py:316`, on a schema-validation failure). The exception
  handler (`agent_sdk.py:271-286`) likewise makes a single attempt and
  returns on failure rather than retrying. There is currently no retry
  concept in the codebase for this event category to describe; a future
  retry mechanism would need its own event schema.
- **Todos** — `SDK-CAPABILITY-MATRIX.md` row 15 records there is **no
  `Todo*` export at all** in the installed SDK, and explicitly warns
  against reading `TaskUpdatedMessage`/`TaskProgressMessage` etc. as todo
  state — those are unrelated background-task exports (row 15's "naming
  trap"). Todo observability is a misconception, not merely unbuilt.
- **Human-input waits** — `query(prompt=AsyncIterable[...])` supports
  programmatic input (`SDK-CAPABILITY-MATRIX.md` row 4, "optional — needed
  only for the human-question queue"), but nothing in Dreamy's code
  constructs a prompt as an async iterable; every call passes a plain
  string prompt.

## Event schema

This is the schema an implementation must satisfy. It is written to be
implementable directly — every field below has a concrete name, type, and
required/optional status. Fields marked **implemented** exist on every
event written by `logging_util._write()` today. Fields marked
**SPECIFIED** do not exist on any event today and require the changes
described in the preceding section.

### Envelope (every JSONL line under `logs/*.jsonl` and `runs/<run_id>.jsonl`)

| Field | Type | Required | Status | Notes |
|---|---|---|---|---|
| `schema_version` | integer | **required** | SPECIFIED | Not present on any event today. Start at `1`. An event without this field, once the schema is versioned, MUST be treated as `schema_version=0` (pre-versioning) by any reader, never rejected outright, so historical JSONL remains readable. |
| `ts_ms` | integer (epoch ms) | required | **implemented** | `logging_util.py:63` |
| `ts_iso` | string (ISO-8601, UTC, `Z` suffix) | required | **implemented** | `logging_util.py:64` |
| `topic` | string, one of `TOPICS` | required | **implemented** | See § "The event stream" caveat: reflects the `TopicLogger`'s bound topic, which can diverge from the event's actual `agent_type` when a context is reused across agents |
| `level` | string enum: `DEBUG` / `INFO` / `WARN` / `ERROR` | required | **implemented** | `logging_util.py:27` |
| `msg` | string | required | **implemented** | Redacted via `redact.redact()` before write |
| `run_id` | string \| null | required (present when a run is active, `null`/absent otherwise) | **implemented** | Set once per `configure_logging()` call |
| `agent_type` | string \| null | optional | **implemented, inconsistently populated** | Present only on events whose call site chose to pass it as a field kwarg; not enforced |
| `project_id` | string \| null | optional | **implemented, inconsistently populated** | Same caveat; absent on pre-project-resolution phases (ingest/correlate/analyze) |
| `call_id` | string \| null | optional | **SPECIFIED for JSONL** (exists today only in `agent_calls` SQLite, never attached to a log line) | Would let a JSONL event be joined back to its `agent_calls` row |
| `trace_id` | string \| null | optional | **SPECIFIED** | Does not exist anywhere in the codebase today. Should identify one top-level run+project+phase span |
| `parent_id` | string \| null | optional | **SPECIFIED** | Populated from `SubagentStop.agent_id` chains once subagents exist; `null` for a top-level event |
| any additional field | JSON-serializable | optional | **implemented** (mechanism), **variable** (content) | `**fields` kwargs are spread directly into the event dict (`logging_util.py:68`) after `redact_dict()`; every field's string values are redacted, but the *set* of fields is caller-chosen and not schema-enforced today |

Field values are redacted at write time (`msg` via `redact.redact()`, all
other string-valued fields recursively via `redact.redact_dict()`), for
events routed through `logging_util`. This does **not** cover the
`agent_calls.error_text` / `agent_calls.output_json` gap described above —
that is a separate write path (`store.insert_agent_call` /
`finish_agent_call`) that bypasses `logging_util` entirely and currently
bypasses `redact.py` too.

### Versioning rule (SPECIFIED)

`schema_version` bumps whenever a required field is added, removed, or has
its type changed. Adding an optional field is **not** a breaking change and
does not require a version bump — this mirrors the compiler-version stamp
precedent in `store.py`'s `prompt_artifacts.compiler_version` column
(migration v4, `store.py:204-215`), which exists specifically so a schema or
behaviour change can be detected on rows produced before it, rather than
silently reinterpreted. A reader encountering an unknown, higher
`schema_version` MUST treat unrecognized fields as opaque and MUST NOT
fail closed on the envelope fields it does recognize — matching the "never
best-effort parse an unknown schema version" posture already established
for agent structured outputs (`AGENT-PIPELINE.md` § "Structured outputs —
implemented").

## Storage model, concretely

| Store | Format | Scope | Redaction applied | Retention |
|---|---|---|---|---|
| `<output_dir>/logs/<topic>.jsonl` | append-only JSONL | All events for one topic, across all runs | `logging_util._write()` — yes | `retention.purge_old_artifacts()`, default 90 days, tombstoned |
| `<output_dir>/runs/<run_id>.jsonl` | append-only JSONL | All events for one run, across all topics | Same as above (identical line written to both files) | Same |
| `state.db :: agent_calls` | SQLite table | One row per paid SDK call | **partial — `error_text`/`output_json` unredacted** (documented gap above) | Never purged by `retention.py`; `state.db` is explicitly excluded (`retention.py:1` module docstring: "never touches state.db, projects/, config.json") |
| `state.db :: agent_events` | SQLite table | Free-form structured event, currently used only by `teacher.py` as a cache | Depends on caller — `teacher.py` does not redact `entry.get("explanation_md", "")` before writing it as `message` | Same — never purged |
| In-memory ring (`_ring`, 500 entries) | `collections.deque` | Process lifetime only | Same redaction as JSONL (same `_write()` call) | Lost on restart, never persisted |

## Orchestration views, concretely

| View | Data source | Update model | Environment status |
|---|---|---|---|
| Runs (home) | `ReadStore.latest_run()`, `.runs_history()`, `.source_stats()`, `.findings()` | Rendered once on `on_mount`; refreshed after a manual run trigger | Unexercised — `textual` not installed |
| Findings | `ReadStore.findings()`, filtered client-side by `severity_filter` | Same | Unexercised |
| Projects | `ReadStore.all_projects()`, `.cost_30d()`, `.project_detail()` | Same | Unexercised |
| Prompts | `ReadStore.prompt_artifacts()`, `coldstart.check_tree()` for live health | Same | Unexercised |
| Schedule | `ReadStore.schedule_state()`, live `launchctl print` output on every render (`schedule.py:63-67`, explicitly "never cached") | Re-rendered after install/uninstall actions | Unexercised |
| Monitor | `ReadStore.topic_events(since_ms=24h ago)` | Rendered once on `on_mount`; **the `[f] follow` label has no bound action** — no polling exists | Unexercised, and the follow behaviour it advertises does not exist even in source |

## What Dreamy must not claim

Per `OBS-005`, this document and every event schema derived from it MUST
NOT claim to expose or persist a model's private chain-of-thought. Nothing
described above does or is designed to. See
`ADR-011-OBSERVABILITY-WITHOUT-HIDDEN-REASONING.md` for the full argument.
The one place this document touches the boundary: streaming deltas
(`include_partial_messages`, `StreamEvent` — `SDK-CAPABILITY-MATRIX.md` row
3, "optional") may drive transient UI once implemented, but the persisted
event stream and `agent_calls.output_json` capture **normalized completed
content only**, never raw intermediate fragments, per
`AGENT-PIPELINE.md` § "Streaming — SPECIFIED." No code path today writes a
`StreamEvent` to disk, because streaming is not wired at all — `call_claude`
consumes the full generator internally and returns only the aggregated
result.

## Open questions this document surfaces, not resolves

1. Should `TopicLogger` bind its topic dynamically from the `agent_type`
   field on each call, instead of at construction? The current fixed-topic
   design is the direct cause of the research/teacher/friends/compiler
   file-attribution gap documented above. This is a design decision for
   whoever implements `trace_id`/`parent_id`, not resolved here.
2. `agent_calls.error_text`/`output_json` redaction gap: fix at the
   `agent_sdk.py` call site (redact before calling `finish_agent_call`), or
   push redaction into `store.finish_agent_call` itself? The latter would
   require `store.py` to import `redact.py`, which today it deliberately
   does not — a decision this document does not make.
3. `Config.log_topics` is dead configuration. Either wire it as a real
   filter in `_write()`, or remove it — leaving it unread is the same class
   of defect the `interval_seconds` type-check comment in `config.py`
   warns against for a different field: a config value that looks load-bearing
   but silently isn't.
