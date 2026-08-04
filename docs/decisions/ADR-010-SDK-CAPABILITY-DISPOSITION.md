# ADR-010 — SDK capability disposition

- **Status:** Accepted
- **Date:** 2026-08-01
- **Requirement IDs:** `SDK-002`, `SDK-003`, `SDK-005`, `SDK-010`

## Context

The Agent SDK ships 145 exports. `docs/research/SDK-CAPABILITY-MATRIX.md`
assigns a disposition to each of the 18 capability groups. This ADR records the
*rule* that produced those assignments, and the gap between assignment and
implementation — the matrix says what each capability is for, not what is built.

## Decision

Disposition is decided by one question, applied in order:

1. **Can this capability mutate a managed target repository, directly or
   transitively?** If yes and it cannot be constrained → `prohibited`.
2. **Does a product invariant fail without it?** If yes → `required`.
3. **Does it improve a working product without changing any invariant?**
   If yes → `optional`.
4. **Is it absent from the installed API surface?** → `not-applicable`.

Rule 1 outranks rule 2. Capability 6 (file checkpointing) is the clean example:
it is undo for edits Dreamy must never make, so a product that needs it has
already violated `SDK-005`. Enabling checkpointing would make the mutation path
*recoverable* rather than *unavailable*, which is the wrong safety model.
Capability 16 (plugin loading) fails the same test: unpinned third-party code
inside the trust boundary is a mutation vector no permission rule can bound.

Rule 4 is a statement about the installed build, not about the SDK. Capabilities
12 (slash commands) and 15 (todo state) have no top-level export in `0.2.128`;
both surface as message content instead. They are `not-applicable` because there
is no API to dispose of — not because the feature is unwanted.

### Constrained-required

Capability 11 (subagents) is `required, constrained`: a subagent is a
privilege-escalation surface, since it can hold a tool set its parent does not.
It is required because compilation needs role isolation, and constrained because
the child's permitted tools must be a subset of the parent's. That subset check
is a Dreamy-side invariant; the SDK does not enforce it.

## Implementation status — measured, not asserted

Ten capabilities are `required`. Four are referenced in `src/dreamy/`: rows
1, 7, 8 and 14.

| # | Capability | Wired | Evidence in `src/` |
|---|---|---|---|
| 1 | Python API surface | yes | `query(` |
| 2 | Permissions | **no** | no `permission_mode`, no `can_use_tool` |
| 5 | Lifecycle hooks | **no** | no `hooks=`, no `HookMatcher` |
| 7 | Structured outputs | yes | `output_format`, `structured_output` |
| 8 | System prompt | yes | `system_prompt` |
| 9 | MCP servers | **no** | no `create_sdk_mcp_server` |
| 10 | Custom tools | **no** | no `tool(`, no `SdkMcpTool` |
| 11 | Subagents | **no** | no `agents=`, no `AgentDefinition` |
| 13 | Skills / setting sources | **no** | no `skills=` reaching an Agent SDK call. Seven files under `src/dreamy/` contain the raw token, named here as paths relative to that root: four Python files in incidental contexts (`cli.py`, `efficacy.py`, `prompt_compiler.py`, `skills_lock.py`), two implementing Dreamy-local skill config and pinning (`config.py`, `skill_pins.py`), and the generated pin manifest `resources/skills.lock` (data, not code). None reaches `claude_agent_sdk`. No `setting_sources` in `src/dreamy/` |
| 14 | Cost accounting | yes | `total_cost_usd`, `max_budget_usd` |

**Correction, 2026-08-02.** Row 13 previously read `yes` on the strength of a
raw grep for the token `skills` across `src/dreamy/`, which matched an
argparse variable name, a source comment, an f-string footer, and a dict
key — none of them a call into the Agent SDK. `setting_sources` has zero
occurrences in `src/dreamy/`; the occurrences elsewhere in the repository are
non-product evidence and documentation, and the disposition is about what
`src/dreamy/` does. The guard now scopes measurement by provenance
(does the hit reach an SDK call) rather than by raw text match; see
`e2e-evidence/forge-260801/phase1/sdk-surface.md` for the full audit.
Reproduce by grepping `src/dreamy/**/*.py` for the symbols in the right column.

The eight mandated custom tools — `dreamy_read_session_excerpt`,
`dreamy_list_project_sessions`, `dreamy_get_git_evidence`,
`dreamy_get_findings`, `dreamy_get_artifact_manifest`,
`dreamy_resolve_evidence_path`, `dreamy_get_skill_manifest`,
`dreamy_submit_human_question` — appear in `AGENT-PIPELINE.md` and in no
executable module. `OBSERVABILITY.md` already records them as "SPECIFIED, not
built"; this ADR agrees and quantifies it.

### What that gap means for safety

It does **not** mean Dreamy can currently mutate a target repository. An AST
scan shows the product never requests `Write`, `Edit`, or `Bash`. But absence of
a request is not a denial: rows 2 and 5 are the surfaces that would *refuse* such
a request if some future code path made one. Until they are wired, `SAFE-001` is
`UNVERIFIED` rather than satisfied — which is exactly what `GATE-MATRIX.md` G9
records. This ADR does not change that verdict.

The distinction is worth stating precisely, because it is easy to over-claim:

- **Established:** no mutation tool is requested today (AST scan).
- **Not established:** a mutation request would be denied (needs row 2 + a real
  `DENIED_BY_PERMISSION` result from a live call).

`tests/sdk_conformance/test_mutation_denial.py` is written to settle the second
point and is opt-in via `DREAMY_RUN_SDK_MUTATION_TEST=1`. It is currently
SKIPPED. A skipped test is not evidence.

## Consequences

`SDK-003` requires a conformance test per *enabled* capability. Six required
capabilities are unwired, so six conformance tests cannot exist yet — G3 and G6
stay BLOCKED, and the reason is now countable rather than narrative.

`SDK-010` requires a missing capability to produce an explicit degraded state.
Six do not, because there is no runtime path to degrade — they are absent at
build time, not unhealthy at call time. When rows 2, 5, 9, 10, 11, and 13 are
wired, each needs a degraded state on top.

The order that follows from rule 1 outranking rule 2: rows 2 and 5 (the
enforcement surfaces) precede rows 9 and 10 (the tools they would govern), which
precede row 11 (the escalation surface that inherits both). Row 13 (skills /
setting sources) carries no such rule-1/rule-2 ordering constraint — it is
unwired by omission rather than by rule — and can land independently of rows
2, 5, 9, 10, and 11. Building tools before the permission layer would create
the request path before the refusal path.
