# AGENT-PIPELINE

Requirements: `SDK-001` … `SDK-010`, `SAFE-002`, `SAFE-003`

How Dreamy drives the Claude Agent SDK. Every behavioural claim about the SDK is
sourced from `docs/research/SDK-CAPABILITY-MATRIX.md`, which records
documentation URLs, retrieval dates, and which claims are documented versus
merely schema-corroborated.

Pinned build: `claude-agent-sdk 0.2.128`, Claude CLI `2.1.220`.

## Implementation status

This document is **part description, part specification**, and the two are
labelled throughout. What `agent_sdk.py` does today, by AST inspection:

| Surface | Status |
|---|---|
| `query()` | **implemented** — `call_claude`, `_collect_query`, `_run_query_sync` |
| `output_format` structured output | **implemented**, with local `_validate_structured` |
| `max_budget_usd` spend cap | **implemented** — `SpendCapExceeded`, `_sum_run_cost`, `_max_call_cost_usd` |
| `agents` (subagent definitions) | **NOT IMPLEMENTED** — no `AgentDefinition`, no `agents=` keyword; the sole `ClaudeAgentOptions` construction (`agent_sdk.py:241-250`) passes `model`/`env`/`cwd`/`max_budget_usd`/`cli_path` only |
| `permission_mode` / `allowed_tools` / `disallowed_tools` | **NOT IMPLEMENTED** |
| `hooks` | **NOT IMPLEMENTED** |
| `can_use_tool` | **NOT IMPLEMENTED** |
| `mcp_servers` + the eight custom tools | **NOT IMPLEMENTED** |
| `ClaudeSDKClient` | **NOT IMPLEMENTED** |

Sections below marked **SPECIFIED** describe required behaviour that does not
exist yet. They are design commitments, not descriptions of running code, and
none may be cited as evidence for an `SDK-*` requirement until built and tested.

## Two execution modes — `query()` implemented, `ClaudeSDKClient` SPECIFIED

| Mode | Used for | Why |
|---|---|---|
| `query()` | Bounded, stateless, schema-constrained analysis | Fresh session per call; no cross-contamination between projects |
| `ClaudeSDKClient` | Interactive/multi-turn, streaming, interruption | Session continuity and `interrupt()` |

`query()` is the default. `ClaudeSDKClient` is used only where session continuity
is genuinely required, because it holds a connection and its lifecycle must be
managed explicitly.

## Isolation — implemented

All SDK imports live in `agent_sdk.py` — verified by AST scan across all 51
modules, exactly one hit. Everything else in the product is SDK-agnostic, so the
`agent` extra being absent degrades agent-backed analysis and nothing else.

## Permissions — SPECIFIED

Documented evaluation order:

```
Hooks → Deny rules → Ask rules → Permission mode → Allow rules → can_use_tool
```

Two consequences that shape the configuration:

- `can_use_tool` is **not** invoked for calls auto-approved by `allowed_tools`,
  allow rules, or `permission_mode`. Listing a tool in `allowed_tools` *and*
  gating it in the callback means the callback never runs.
- Hooks run **first**, and apply even under `bypassPermissions`. That makes a
  `PreToolUse` hook a genuine backstop rather than decoration.

Production configuration:

| Setting | Value | Reason |
|---|---|---|
| `permission_mode` | `"dontAsk"` | Never `bypassPermissions` (ignores `allowed_tools`); never `acceptEdits` (auto-approves writes and destructive `Bash`) |
| `allowed_tools` | Dreamy's read-only MCP tools only | Least privilege |
| `disallowed_tools` | `["Write", "Edit", "NotebookEdit", "Bash"]` | **Bare names** remove the tool from context entirely; a scoped rule like `Bash(rm *)` would leave `Bash` available |
| `can_use_tool` | Path/permission callback | Backstop for anything reaching the prompt |
| hooks | `PreToolUse` deny-by-default | Runs before deny rules |

## Subagents are an escalation surface — SPECIFIED constraint

Documented behaviour, not inference: a subagent's `permissionMode` **can escalate
to `bypassPermissions`** when the parent runs `default`, `dontAsk`, or `plan`.
Only a parent already in `bypassPermissions`, `acceptEdits`, or `auto` blocks
per-subagent override. And `AgentDefinition.mcpServers` accepts *inline* server
definitions, so a subagent can be handed tools the parent never registered.

A "safe parent session" therefore does not bound its children. Every
`AgentDefinition` Dreamy ships MUST:

- set `tools` to an explicit narrow allowlist,
- **never** set `permissionMode`,
- **never** set `mcpServers`.

UNRESOLVED: whether `strict_mcp_config` filters subagent-level inline
`mcpServers`. Until settled, assume it does not.

## Custom tools — SPECIFIED

Eight narrow, typed, read-only tools hosted through an in-process SDK MCP server,
exposed as `mcp__dreamy__<name>`:

`dreamy_read_session_excerpt`, `dreamy_list_project_sessions`,
`dreamy_get_git_evidence`, `dreamy_get_findings`, `dreamy_get_artifact_manifest`,
`dreamy_resolve_evidence_path`, `dreamy_get_skill_manifest`,
`dreamy_submit_human_question`.

Each returns bounded, redacted, schema-validated data. None exposes arbitrary
filesystem paths, arbitrary SQL, shell commands, file writes, patch application,
git mutation, credential values, or raw unrestricted transcripts.

**`ToolAnnotations` are not a security mechanism.** The SDK reference states
clients should not rely on them for security decisions; a tool declaring
`readOnlyHint=True` can still write to disk. Dreamy's read-only posture will
rest on the tool implementations, path canonicalization, and permission
configuration — never on an annotation. All three are **SPECIFIED**, not yet
built, so this describes the intended boundary rather than a current one.

## Hooks — SPECIFIED

The 10 hook events this build accepts match the 10 documented for Python exactly:
`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`,
`SubagentStart`, `SubagentStop`, `PreCompact`, `Notification`,
`PermissionRequest`.

Four events the product brief asks for do not exist as Python callbacks:

| Wanted | Reality |
|---|---|
| Session start/end | TypeScript-only as callbacks. Derive from the stream: first message, final `ResultMessage` |
| Cost updates | Never a hook. Read `ResultMessage.total_cost_usd` / `.usage` / `.model_usage` |
| Agent completion | Covered by `SubagentStop` (`agent_id`, `agent_transcript_path`, `agent_type`) |
| Redaction before persist | **No such hook.** See below |

Hooks have bounded execution time and fail closed for permission and safety
checks. Observability-hook failure may degrade with an alert, but only where it
cannot weaken a safety invariant.

### The redaction gap

No documented hook fires before local transcript persistence.
`PostToolUse.updatedToolOutput` changes only what Claude sees, not what is
written to disk, and by `PostToolUse` any real mutation has already happened.

`SAFE-002` therefore cannot be delegated to the SDK. Dreamy redacts at its own
persistence boundary in `redact.py`, which has zero internal dependencies
precisely so it cannot be bypassed by a cycle through a higher layer.

## Structured outputs — implemented

Every agent role has a versioned JSON Schema supplied via `output_format`.
Results are read from the documented result field, then validated **again**
locally — an agent that returns malformed output is a failure, not a warning.
Unknown schema versions are rejected rather than best-effort parsed.

## Cost accounting — implemented

`max_budget_usd` is checked **after** spend, not pre-flight, and compares against
the same client-side price-table estimate as `total_cost_usd`. Subagent-halting
enforcement is guaranteed only on CLI ≥ 2.1.217 (pinned: 2.1.220).

Dreamy treats it as a best-effort ceiling and enforces its own hard stop. A cap
presented as exact would be a false guarantee.

## Streaming — SPECIFIED

`include_partial_messages` yields `StreamEvent` messages. Deltas may drive
transient UI; **normalized completed content is what gets persisted**, not raw
fragments, unless diagnostic retention is explicitly enabled.

Python's `StreamEvent` lacks TypeScript's `ttft_ms` — an acknowledged parity gap,
so time-to-first-token is not available here.

## Version pin rationale

`0.2.127` fixed a bug where `PreToolUse` hooks were **silently bypassed** for
background-task tool calls due to premature stdin closure. The pinned `0.2.128`
carries that fix; any older pin does not. Loosening the pin below `0.2.127`
would silently disable the backstop this design depends on.
