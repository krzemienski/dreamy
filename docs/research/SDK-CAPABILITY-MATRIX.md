# SDK-CAPABILITY-MATRIX

Requirement: `SDK-002` — every documented SDK capability must receive an explicit
disposition.

## Review status

| Pass | Source | Status |
|---|---|---|
| 1 | Official documentation pages | **DONE WITH OPEN ITEMS** — retrieved 2026-08-01 |
| 2 | Installed package exports, symbols, signatures | **DONE** — `tools/sdk-inventory.py` |
| 3 | Official repository examples | **DONE** |
| 4 | Changelog | **DONE** |

Environment: `claude-agent-sdk 0.2.128`, Claude CLI `2.1.220`, Python `3.11.6`,
Darwin arm64. Verify in the hash-locked environment, not the ambient
interpreter — see `SDK-VERSION-LOCK.md` § "Which environment to verify in".

### Open items from pass 1

Every capability has a disposition, but four questions were not settled by the
documentation fetched. Listed here rather than buried, because a flat "DONE"
would hide them:

| Capability | Unsettled question | Effect on disposition |
|---|---|---|
| 8 — System prompt | CLI error text/behaviour for a missing or malformed `SystemPromptFile` path | None. Affects failure-behaviour column, not the `required` call |
| 11 — Subagents | Whether `strict_mcp_config` filters subagent-level inline `mcpServers` | Assume **not**. Drives the "never set `mcpServers`" rule in Row 11 |
| 17 — Sandboxing | Whether `SandboxSettings` gates filesystem writes, or only the confirmed network `allowedDomains`/`deniedDomains` | Row 17 stays **optional**; it cannot be relied on as a write boundary until settled |
| 12 — Slash commands | Behaviour when an unrecognised `/foo` is dispatched as a prompt string (literal text vs CLI error) | None. Row 12 is **not-applicable** either way |

None of the four changes an assigned disposition; each is recorded so a later
reader can close it rather than rediscover it.

**Dispositions are assigned. Conformance tests are NOT yet written**, so this
file does not by itself satisfy G3 — see Exit criteria.

## Dreamy's constraint

Read-only cross-harness reconciler. The installed product must be physically
incapable of modifying managed target repositories: no `Write`, no `Edit`, no
unrestricted `Bash`, never `bypassPermissions`. Deterministic ingestion,
correlation, git evidence, compilation, reporting, and safety behaviour must all
keep working with the SDK absent.

## Dispositions

| # | Capability | Installed symbol / field | Disposition | Rationale |
|---|---|---|---|---|
| 1 | Python API surface | `claude_agent_sdk` (145 exports) | **required** | `query()` for bounded analysis, `ClaudeSDKClient` for multi-turn orchestration |
| 2 | Permissions / authorization | `permission_mode`, `can_use_tool`, `CanUseTool` | **required** | The primary enforcement surface; see Permission ordering |
| 3 | Streaming output | `include_partial_messages`, `StreamEvent` | **optional** | Transient UI only; normalized content is persisted, not raw fragments |
| 4 | Programmatic user input | `query(prompt=AsyncIterable[...])` | **optional** | Needed only for the human-question queue |
| 5 | Lifecycle hooks | `hooks`, `HookMatcher`, `include_hook_events` | **required** | Defense in depth — never the sole boundary |
| 6 | File checkpointing / rollback | `enable_file_checkpointing`, `rewind_files()` | **prohibited** | Undo for edits Dreamy must never make — see below |
| 7 | Structured outputs | `output_format`, `ResultMessage.structured_output` | **required** | Every agent role has a versioned JSON Schema |
| 8 | System-prompt configuration | `system_prompt`, `SystemPromptPreset` | **required** | Prompt provenance must be recorded |
| 9 | MCP servers (external + in-process) | `create_sdk_mcp_server`, `strict_mcp_config` | **required** | Hosts Dreamy's eight narrow read-only tools |
| 10 | Python custom tools | `tool(...)`, `SdkMcpTool` | **required** | Typed, bounded, redacted replacements for raw filesystem access |
| 11 | Subagents | `agents`, `AgentDefinition`, `SubagentStart/Stop` | **required, constrained** | Privilege-escalation surface — see Row 11 |
| 12 | Slash-command discovery | *(no top-level export)* | **not-applicable** | Surfaces as `SystemMessage.data['slash_commands']` on the init message |
| 13 | Runtime skills / setting sources | `skills`, `setting_sources` | **required** | Skill provenance and pinning (ADR-009) |
| 14 | Cost and usage accounting | `total_cost_usd`, `model_usage`, `max_budget_usd` | **required** | Spend cap — read the caveat below |
| 15 | Todo / task state | *(no top-level export)* | **not-applicable** | Streams as `ToolUseBlock` in `AssistantMessage.content` |
| 16 | Plugin loading | `plugins`, `SdkPluginConfig` | **prohibited** | Unpinned third-party code inside the trust boundary |
| 17 | Sandboxing | `sandbox`, `SandboxSettings` | **optional** | Additional layer; insufficient as a sole boundary |
| 18 | Session persistence / forking | `session_store`, `fork_session` | **optional** | Dreamy reads other harnesses' sessions; its own are incidental |

## Permission ordering

Documented evaluation order, confirmed 2026-08-01:

```
Hooks → Deny rules → Ask rules → Permission mode → Allow rules → can_use_tool
```

`can_use_tool` is **not** invoked for calls auto-approved by `allowed_tools`,
allow rules, or `permission_mode`. A callback listed alongside an allow rule for
the same tool never runs — the allow rule wins first.

Configuration for a read-only product:

- `permission_mode="dontAsk"` (verified present in this build's `PermissionMode`
  literals: `default`, `acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `auto`)
- `allowed_tools` limited to read-only tools
- `disallowed_tools` with **bare names** — `"Bash"` removes the tool from context
  entirely, whereas a scoped rule `"Bash(rm *)"` leaves it available
- a `PreToolUse` hook as an independent backstop: hooks run *first*, and apply
  even under `bypassPermissions`

Never `bypassPermissions` (documented to ignore `allowed_tools`) and never
`acceptEdits` (auto-approves file writes and destructive `Bash`).

## Row 11 — subagents are an escalation surface

A first review recorded subagents as "optional, not load-bearing" and assumed a
subagent could only *tighten* the parent's permissions. Documentation review
(`agent-sdk/permissions`, subagent-inheritance warning; `sub-agents`
`permissionMode` frontmatter — retrieved 2026-08-01) contradicts that on two
points.

Evidence separation matters here. Inspecting the installed `AgentDefinition`
dataclass shows only that these fields exist and what values they accept — it is
schema corroboration, not proof of runtime inheritance behaviour:

```
permissionMode  Optional[Literal['default','acceptEdits','plan','bypassPermissions',...]]
mcpServers      list[str | dict[str, Any]] | None
```

The escalation semantics below come from the documentation; the schema merely
confirms this build exposes the surface those docs describe.

1. **`permissionMode` can escalate** (per permissions docs). A subagent may
   declare `bypassPermissions` while the parent runs `default`, `dontAsk`, or
   `plan`. Only a parent already in `bypassPermissions`, `acceptEdits`, or
   `auto` blocks per-subagent override. A "safe parent session" does **not**
   bound its children.
2. **`mcpServers` can add tools** (per the Python reference field table). The
   `dict` branch is an inline server definition, not a reference to a
   parent-registered server, so a subagent can be handed MCP tools the parent
   never registered — independently of the `tools` / `disallowedTools` ceiling
   on built-ins.

Neither point has been exercised at runtime here. Both warrant a conformance
test before Dreamy ships subagents.

Dreamy's mutation-incapability therefore cannot rest on inheritance. Every
`AgentDefinition` Dreamy ships MUST:

- set `tools` to an explicit narrow allowlist (or `[]`),
- **never** set `permissionMode`,
- **never** set `mcpServers`,

with OS-level sandboxing and managed settings as the outer boundary. A
`PreToolUse` hook remains the backstop, since hooks run before deny rules and
apply even under `bypassPermissions`.

UNRESOLVED: whether `strict_mcp_config` filters subagent-level inline
`mcpServers` entries. Marked `[INFERENCE]` by the reviewer, not confirmed from
documentation text. Until settled, assume it does **not**.

## Row 10 — `ToolAnnotations` are hints, never enforcement

The SDK reference states plainly that annotations are optional hints and that
clients should **not** rely on them for security decisions (retrieved
2026-08-01). A tool declaring `readOnlyHint=True` can still write to disk: the
annotation describes intent to the model and constrains nothing at runtime.

Dreamy's read-only guarantee must therefore never cite annotations as its
mechanism. Annotations may be set for model-facing clarity; the actual boundary
is the tool implementation, path canonicalization, and the permission
configuration.

## Row 6 — why checkpointing is prohibited

The reason is Dreamy's contract, **not** an SDK constraint. That distinction was
tested rather than assumed:

```
enable_file_checkpointing=True + permission_mode="dontAsk"  → constructed OK
  with allowed_tools=["Read","Grep"], disallowed_tools=["Write","Edit","NotebookEdit","Bash"]
same with permission_mode="default" / "plan"                → constructed OK
```

The SDK does not require write permission to enable checkpointing. An initial
review concluded it did, reasoning from every documented example pairing
`enable_file_checkpointing=True` with `acceptEdits` — but that shows convention,
not API coupling.

So the accurate statement: checkpointing is an undo facility for edits Dreamy is
forbidden to make. It is prohibited because it is **pointless and misleading**
here — shipping it would imply a mutation capability the product must not have —
not because the SDK rejects the combination.

## Hook coverage vs. the product brief

The 10 documented Python hook events exactly match the 10 this build accepts:
`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`,
`SubagentStop`, `SubagentStart`, `PreCompact`, `Notification`, `PermissionRequest`.

Four events named in the product brief have **no Python callback hook**:

| Brief item | Reality |
|---|---|
| Session start / end | TypeScript-only as callbacks. Python must use shell-command hooks via `setting_sources=["project"]`, or derive from the stream (first message / final `ResultMessage`) |
| Cost updates | Never a hook. Read `ResultMessage.total_cost_usd` / `.usage` / `.model_usage` |
| Agent completion | Covered — `SubagentStop` carries `agent_id`, `agent_transcript_path`, `agent_type` |
| Redaction before persist | **UNRESOLVED.** No documented hook fires before local transcript persistence. `PostToolUse.updatedToolOutput` changes only what Claude sees, not what is written to disk |

The redaction gap is load-bearing for `SAFE-002`. Dreamy must redact at its own
persistence boundary; it cannot delegate that to a hook.

## Row 14 — spend-cap accuracy

`max_budget_usd` is checked **after** spend, not pre-flight, and compares against
the same client-side price-table estimate as `total_cost_usd`. Subagent-halting
enforcement is guaranteed only on CLI ≥ 2.1.217 (installed: 2.1.220).

A cap that silently overshoots is a product risk. Dreamy must treat it as a
best-effort ceiling and enforce its own hard stop, not present it as exact.

## Changelog findings

`0.2.127` — one release before the pinned build — fixed a security-relevant bug
where `PreToolUse` hooks were **silently bypassed** for background-task tool
calls due to premature stdin closure. The installed `0.2.128` carries that fix;
any older pin would not. This is a concrete argument against loosening the pin.

## Documentation disagreements observed

1. `permissions.md` documents `CLAUDE_SDK_CAN_USE_TOOL_SHADOWED` as
   TypeScript-only, but changelog `0.2.111` added an equivalent Python runtime
   warning the page never mentions.
2. `user-input.md` tells `can_use_tool` implementers to return a `defer`
   decision, but `defer` is only valid as a `PreToolUse` hook output field — not
   a `PermissionResultAllow` / `PermissionResultDeny` return.
3. Python `StreamEvent` lacks TypeScript's `ttft_ms` field (acknowledged parity
   gap).
4. File checkpointing has **zero** examples in the shipped `examples/` tree
   despite a complete inline tutorial in `file-checkpointing.md`.

## Naming trap

The `Task*` exports — `TaskNotificationMessage`, `TaskProgressMessage`,
`TaskStartedMessage`, `TaskUpdatedMessage`, `TaskBudget`, `TaskUsage` — belong to
the **background-task** family and are unrelated to todo state (row 15). There
are zero `Todo*` exports. Reading `TaskUpdatedMessage` as todo state would be a
name-based inference of exactly the kind this review exists to prevent.

## Exit criteria

Dispositions are assigned, but G3 additionally requires a real executable
conformance test per enabled capability under `tests/sdk_conformance/`, with
captured evidence. Those are not yet written. **G3 does not pass from this file
alone.**
