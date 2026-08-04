# ADR-011 — Complete operational visibility without collecting hidden reasoning

- **Status:** Accepted
- **Date:** 2026-08-01
- **Requirement IDs:** `OBS-001`, `OBS-002`, `OBS-003`, `OBS-004`, `OBS-005`
- **Related:** `docs/architecture/OBSERVABILITY.md`, `docs/architecture/AGENT-PIPELINE.md`,
  `docs/research/SDK-CAPABILITY-MATRIX.md`, `docs/research/SDK-VERSION-LOCK.md`

## Context

`OBS-005` states: Dreamy MUST NOT claim to expose or persist hidden
chain-of-thought. Taken alone, that reads as a prohibition — a boundary
Dreamy stays inside. It is not that. It is a description of what "complete
observability" means for an agentic system, and the two other readings of
completeness are both wrong.

The first wrong reading: complete observability means capturing everything
the model internally computed on the way to an answer, including its
private reasoning trace, and a product that doesn't do this is
incomplete. This is the reading a naive interpretation of "full visibility
into the agent pipeline" invites, and it is the reading this ADR rejects
with an argument, not a policy statement.

The second wrong reading, subtler and more relevant to this specific
codebase: complete observability means every operationally relevant signal
is captured. Under this reading `OBS-005` is a non-issue — Dreamy simply
doesn't do the thing it's prohibited from doing, so there's nothing to
decide. `docs/architecture/OBSERVABILITY.md`, written alongside this ADR,
shows that reading is also too easy. Large parts of `OBS-001`
through `OBS-004` are **not yet implemented**: trace/parent correlation
identifiers don't exist, permission-decision events don't exist because no
permission hook is wired, todo events don't exist because the SDK exposes
no todo surface at all, and a documented `agent_calls`/`agent_events`
write path (`agent_sdk.py`'s exception and success branches) persists
`error_text` and `output_json` to SQLite without passing through
`redact.py` — a real gap, not a hypothetical one. An ADR that argued
"Dreamy achieves complete visibility" while ignoring these gaps would be
lying to whoever reads it next. This decision is scoped narrowly: it
argues that the *design target* — full visibility into what an agent did,
without hidden reasoning — is coherent and correct, and separately records
that the target is not yet fully built. Those are different claims and
must not be conflated.

## What is actually observable

Every fact about what an agent call *did*, as opposed to *thought*, is a
message-stream or database fact, not a hidden internal:

- **Tool calls** — every tool invocation and its result is a message on
  the stream, addressable and (once wired) hookable via `PreToolUse` /
  `PostToolUse` / `PostToolUseFailure`. This is documented behaviour, not
  aspiration — `SDK-VERSION-LOCK.md` confirms these three hook events are
  accepted by the installed `0.2.128` build.
- **Costs** — read from `ResultMessage.total_cost_usd`, `.usage`, and
  `.model_usage`. Never a hook, because cost is a settled fact only once
  the call finishes, but it is a hard number, sourced from the SDK's own
  accounting, not inferred from anything the model "thought."
- **Timings** — `ResultMessage.duration_ms` and `.duration_api_ms` are
  documented fields (`SDK-VERSION-LOCK.md` § `ResultMessage` fields). Start
  and end of a session are derivable from stream position (first message,
  final `ResultMessage`) without any hidden internal state.
- **Retries and failures** — `stop_reason`, `is_error`, `errors`,
  `api_error_status`, `terminal_reason` are all documented `ResultMessage`
  fields. Today's implementation deliberately makes zero retries
  (`agent_sdk.py:316`, `"No silent retry — the model could return the same
  shape"`), which is itself a fully observable, logged decision — the
  absence of a retry is recorded as clearly as a retry would be.
- **Structured outputs** — every agent role returns a versioned JSON
  Schema via `output_format`, validated locally a second time after the
  SDK's own validation (`AGENT-PIPELINE.md` § "Structured outputs —
  implemented"). This is the model's *answer*, in a shape a program can
  consume — not its scratch work.
- **Permission decisions** (once wired) — the documented evaluation order
  (`Hooks → Deny rules → Ask rules → Permission mode → Allow rules →
  can_use_tool`) means every allow/deny outcome is attributable to a named
  stage, not opaque.
- **Subagent lifecycle** (once used) — `SubagentStart`/`SubagentStop`
  carry `agent_id`, `agent_transcript_path`, `agent_type`. A subagent's
  entire externally visible behaviour — what it called, what it cost, how
  it ended — is captured by this surface without needing its internal
  deliberation.

That is a materially complete picture of *what happened*: what was called,
what it cost, how long it took, whether it succeeded, what it returned,
and — once the SPECIFIED pieces in `OBSERVABILITY.md` are built — which
permission gate approved each step and which parent call spawned which
child. None of it requires reading the model's private reasoning.

## What is deliberately not collected, and why

Anthropic's own documented product behaviour is the starting fact here,
not a Dreamy invention: the model's internal reasoning trace is not a
stable, structured, API-exposed artifact in the way `ResultMessage` fields
are. Even where a reasoning-adjacent signal exists in a stream (a
`StreamEvent` partial-message delta), it is explicitly a **transient
rendering aid**, not a persisted record — `AGENT-PIPELINE.md` says this
plainly: *"Deltas may drive transient UI; normalized completed content is
what gets persisted, not raw fragments, unless diagnostic retention is
explicitly enabled."* Dreamy inherits this posture by construction, not by
extra policy: nothing in `agent_sdk.py` subscribes to `StreamEvent` at
all today (`include_partial_messages` is never set), so there is currently
no code path that could persist a reasoning fragment even accidentally.
When streaming is eventually wired for UI responsiveness, the same rule
applies — deltas render, only the final normalized `ResultMessage` content
is written to `agent_calls.output_json` or the event log.

Three independent reasons this is a strength, not a limitation:

**1. Chain-of-thought is not a reliable audit artifact.** A model's
verbalized reasoning is a generated text sequence subject to the same
hallucination and post-hoc rationalization risks as any other model
output — it is not guaranteed to be a faithful trace of the actual
computation that produced the answer. Treating it as ground truth for
"why did the agent do X" would give Dreamy's operator a false sense of
explainability: a plausible-sounding narrative that may not correspond to
the actual decision process. The things Dreamy *does* capture — the tool
called, the cost incurred, the schema-validated output returned, the
permission gate that allowed it — are facts about what happened,
independently verifiable against the SDK's own accounting and against the
git-evidence layer described in `ARCHITECTURE.md`. A reasoning transcript
is not independently verifiable in the same way; it is exactly the kind of
"provenance" `evidence_projection.py`'s entire sanitization apparatus
(stripping ephemeral runtime paths, defanging quoted skill references)
exists to be suspicious of in mined prose. Building an audit trail on an
artifact that cannot itself be audited would be a category error.

**2. It is not Dreamy's data to keep.** Dreamy's trust boundary, laid out
in `ARCHITECTURE.md`, is a read-only reconciler across harnesses that must
never mutate a managed repository and must never leak a secret. A model's
internal reasoning is the vendor's generation-time internal state, exposed
(where it is exposed at all) as an ephemeral UI convenience, not as a
documented, versioned, retained data product. Persisting it would mean
Dreamy silently becomes a long-term store for content whose format,
stability, and privacy properties are outside Dreamy's control and outside
Anthropic's documented API contract. `redact.py`'s entire design
philosophy — deterministic, zero-dependency, known-value redaction because
"a pattern cannot fix this" — exists precisely because Dreamy has already
been burned once (the bare-hex CLI secret in `state.db`, `redact.py:30-49`)
by treating a wide, poorly-specified capture surface as safe by default.
Chain-of-thought is a wider, less-specified surface than anything Dreamy
has captured before. The correct posture, consistent with every other
decision in this codebase, is: capture what is documented, structured, and
stable; do not capture what is transient, undocumented, and out of
Dreamy's control.

**3. Structured facts scale; free-text reasoning does not.** Every
observability surface Dreamy builds — the JSONL event stream, the
`agent_calls` ledger, the correlation identifiers `OBS-002` requires — is
designed to be queried, aggregated, and correlated across runs, projects,
and agents. A cost figure, a tool name, a stop reason, and a schema-validated
structured output are all first-class values a SQL query or a
JSONL grep can operate on. A block of free-text reasoning is not: it
resists aggregation, cannot be joined against `run_id`/`project_id`
without an additional (fallible) extraction step, and would dominate
storage volume for zero queryable benefit. The 90-day default retention in
`retention.py`, and the deliberate exclusion of `state.db` from that
purge, both assume the retained data is small, structured, and durable —
assumptions a raw reasoning corpus would violate immediately.

## The argument, restated plainly

`OBS-005` is not a constraint Dreamy works around. It states the actual
shape of the problem: an agent's *observable behaviour* — what it called,
what it cost, what it returned, what permitted it, what parent spawned it
— is a complete, structured, verifiable record of everything an operator,
an auditor, or a future incident investigation needs. An agent's *private
reasoning* is none of those three things (structured, verifiable,
stable), and a system that tried to capture it anyway would trade a
reliable audit trail for an unreliable one, would take on data it has no
right or ability to govern, and would build an observability layer that
cannot actually be queried at scale. Completeness, correctly defined, is
"every fact about what happened is captured" — not "every token the model
emitted internally is captured." Dreamy targets the former. That is a
design strength: it means every piece of `OBSERVABILITY.md`'s event schema
is a fact a `WHERE` clause can filter on, not a paragraph a human has to
read and interpret to trust.

## Consequences

- Dreamy's event schema (`OBSERVABILITY.md`) has no field for reasoning
  text, chain-of-thought, or raw stream deltas, and none should ever be
  added. Any future request to add one should be read against this ADR
  first.
- Streaming, when implemented, persists only normalized completed content
  (`AgentResult.content`, `.structured_output`), never raw
  `StreamEvent` fragments, matching `AGENT-PIPELINE.md`'s existing
  SPECIFIED contract.
- This ADR does not excuse the implementation gaps recorded in
  `OBSERVABILITY.md` — trace/parent correlation, permission-decision
  events, and the `agent_calls.error_text`/`output_json` redaction gap are
  real, tracked, and unaffected by this decision. `OBS-005` is satisfied
  today by construction (nothing captures reasoning because nothing
  subscribes to the surface that would carry it); `OBS-001` through
  `OBS-004` remain partially unmet and are tracked separately.
- If Anthropic ever documents chain-of-thought as a stable, versioned,
  retrievable API field — rather than an ephemeral streaming convenience —
  this decision should be revisited on its own merits, not assumed to
  still hold. Nothing here is a permanent technical impossibility; it is a
  reasoned position against the currently documented SDK surface,
  consistent with the "verify in the hash-locked environment, don't assume
  parity" discipline `SDK-VERSION-LOCK.md` establishes for every other
  claim in this codebase.
