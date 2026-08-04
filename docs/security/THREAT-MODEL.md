# THREAT-MODEL

Requirements: `SAFE-001`, `SAFE-002`, `SAFE-003`

## Scope and method

Dreamy reads six agent-harness stores and a set of managed git repositories,
correlates that data, optionally sends redacted excerpts to a model for
analysis, and writes derived artifacts to its own output directory. It never
edits a managed repository. This document threat-models that pipeline
asset-by-asset. For each threat: which STRIDE category it falls under, the
mitigation, and whether the mitigation is **IMPLEMENTED** (verified in the
current source tree) or **SPECIFIED** (a requirement the design commits to,
not yet built or not yet enforced at runtime). Nothing in this document is
promoted to IMPLEMENTED without a corresponding read of the code that does
it; see the citation on every row.

Companion documents: `PERMISSION-POLICY.md` (the concrete tool/path
allow-deny policy this model assumes), `docs/architecture/ARCHITECTURE.md`
(trust boundaries and layering), `docs/research/SDK-CAPABILITY-MATRIX.md`
(the SDK-level evidence behind several rows below).

## Assets

| Asset | Where | Trust level |
|---|---|---|
| Harness transcripts | `~/.claude/projects`, `~/.codex`, `~/.omp`, OpenCode SQLite, Nine Router history | Read-only input |
| Secrets (API keys, CLI tokens) | Environment variables, `~/.9router/auth/cli-secret` | Never persisted |
| Dreamy's own tools | `agent_sdk.py`, agent role modules under `agents/` | Execution surface |
| Managed target repositories | Git checkouts the sessions were about | Read-only, git plumbing only |
| MCP servers / custom tools | Not present in this build (`SDK-CAPABILITY-MATRIX.md` row 9/10) | N/A today |
| Plugins | `~/.claude/plugins/` (only read for skill-pin lookup) | Prohibited as an execution surface |
| Prompts sent to the model | Compiled in `agent_sdk.py` callers | Contains redacted excerpts only |
| Dreamy's own persisted state | `~/.local/share/dreamy/{state.db, acceptance-runs}` | Read-write, Dreamy-owned |

## 1. Transcripts (harness stores)

**Asset:** JSONL/SQLite session records under each harness's own directory.

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| A malicious or malformed transcript entry corrupts ingestion or crashes the run | Denial of Service | Connectors parse defensively per-line; a bad record produces an `error` `SourceRecord`, not a crash. `content_excerpt` fields pass through `redact()` before persistence. | **IMPLEMENTED** — `src/dreamy/connectors/{claude,codex,omp_pi,opencode}.py`; see the next two rows for the malformed-*directory-name* case |
| A transcript path resolves outside the expected harness home via crafted directory names | Spoofing / Tampering | `pathdecode.py` decodes flattened project-directory names with a filesystem-aware greedy walk against directories that actually exist on disk, rather than a blind `replace('-', '/')` — this defeats naive slash-guessing attacks on hyphenated real directory names. | **IMPLEMENTED** — `src/dreamy/pathdecode.py`; see the next row for the unresolved-tail traversal case |
| A harness project-directory name with a crafted `-..` unresolved tail could decode to a path containing a literal `..` segment, escaping the walked directory when used as a `git` subprocess `cwd` or in `Path.is_dir()` checks | Spoofing, Tampering, Elevation of Privilege | `_decode_under`'s unresolved-tail branch raises `ValueError` when the leftover leaf equals `.`/`..` or contains an embedded `/` or `\`. A separate `_is_unsafe_cwd()` helper provides defense-in-depth on any resulting cwd string, rejecting a non-`/`-rooted path, embedded NUL/backslash, or a `..` path segment — two independent layers, not one relying on the other. Covered by `tests/unit/test_pathdecode_traversal.py`, including the layout-dependent case where a `..` tail follows a matched real directory (raises only when the matched directory exists; treated as one literal filename otherwise — both branches pinned by separate tests so a refactor cannot silently change which one runs). | **IMPLEMENTED** — `src/dreamy/pathdecode.py::_decode_under`, `::_is_unsafe_cwd` |
| A connector's integration of the decoder's traversal-rejection `ValueError` could fail to catch it, aborting ingestion of every remaining file in that scan rather than skipping the one crafted directory | Denial of Service | `claude.py`'s call site wraps the decode in `try/except ValueError`, falling back to an empty `cwd` with a warning; `omp_pi.py`'s call site does the same. Covered by `tests/integration/test_connector_hostile_dirs.py`, whose cases (one per connector) pass. | **IMPLEMENTED** — `src/dreamy/connectors/{claude,omp_pi}.py` |
| Secrets embedded in transcript content reach Dreamy's own database | Information Disclosure | `redact()` runs at ingestion, before any excerpt is stored. Note: this covers content Dreamy itself persists — §7 covers a distinct, still-open threat concerning SDK-side transcript persistence Dreamy does not control. | **IMPLEMENTED** — `src/dreamy/redact.py`; see §7 for the separate SDK-side gap |
| A harness store is more permissive than Dreamy's own reads require, and Dreamy widens that exposure further | Information Disclosure | Dreamy never copies, re-serves, or relaxes permissions on harness-store content — it reads a bounded trailing window and persists only redacted excerpts into its own `state.db`, never republishing the source file itself at a wider permission level. Verified by inspection: no connector chmods, copies, or symlinks a harness-store path into a more widely readable location. | **IMPLEMENTED** — `src/dreamy/connectors/*.py`; the harness's own file permissions remain the operator's responsibility, unaffected by Dreamy |

## 2. Secrets

**Asset:** Live credential values — the Nine Router CLI secret, `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY` — sourced from environment variables
and `~/.9router/auth/cli-secret`.

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| Regex-shaped secret patterns (API-key prefixes, PEM blocks, DB URLs) leak into stored content | Information Disclosure | `_FULL_REPLACE` / `_PREFIX_KEEP` pattern set matches on literal prefixes (`sk-`, `Bearer`, `tok_`, `AKIA`, `gh?_`) and preceding keywords (`token=`, `api_key:`) | **IMPLEMENTED** — `src/dreamy/redact.py` |
| A credential with no recognizable shape (bare hex, no prefix) is unanchored and survives pattern matching | Information Disclosure | Known-value redaction: the live secret is read from its own source (env var or `~/.9router/auth/cli-secret`), cached, and stripped by exact string occurrence rather than pattern — a strategy chosen specifically because a 64-character lowercase-hex secret is byte-indistinguishable from a SHA-256 digest, which this pipeline stores everywhere as content fingerprints. A broader regex would shred legitimate hash content instead of catching the secret. | **IMPLEMENTED** — `src/dreamy/redact.py::_load_known_secrets`, `_known_secrets` cache |
| **This exact gap already occurred once.** The 9router CLI secret reached `state.db` and `state.db-wal` while the pattern-based canary check still reported success, because the canary is `sk-ant-`-shaped and the real token isn't. Recorded as defect D11, caught by `VG-S2 iso_token_hits`. | Information Disclosure | Fixed by the known-value mechanism above. Documented here as the reason known-value redaction exists, not as an open risk — see the comment block at the top of `redact.py` for the full account. | **IMPLEMENTED** (post-fix); historical incident preserved as design rationale |
| A secret is written to a debug print, log, or acceptance artifact by human or agent error during development | Information Disclosure | Operational rule, not code: `gotchas.md` mandates verification-by-boolean-and-length only, never a value or prefix slice; acceptance evidence must be grepped for the token before a gate is marked PASS. | **SPECIFIED** — this is a process control, not an enforced runtime check; nothing in the codebase currently scans acceptance artifacts for leaked secrets before admission |
| Any classified redacting `Store` writer persists untrusted free text (harness content, router-log fields, connector-derived metadata such as a git remote URL) unredacted, OR a hostile non-string value from an external row (a SQLite BLOB, a malformed log's numeric field) crashes ingestion when passed to a redaction call that assumes a `str`. Found first as one reported column at a time (`finish_agent_call`, then `insert_agent_event`/`insert_finding_with_provenance`, then `insert_router.endpoint` during this review), which is exactly the failure mode this row exists to close: fixing sites as they're reported leaves the boundary itself unverified. | Information Disclosure, Denial of Service | Every caller-supplied untrusted free-text binding in a classified redacting writer now goes through `redact_text()` (`redact.py`), a type-safe wrapper handling the types actually arriving from external, type-unguaranteed sources — `str` redacted directly; `bytes`/`bytearray` (a SQLite-backed source can return a BLOB that SQLite stores verbatim) UTF-8-decoded then redacted; `None`/`int`/`float`/`bool` passed through unchanged; anything else `repr()`'d then redacted rather than raising `TypeError` mid-ingestion, which would otherwise turn a redaction fix into an availability failure. Applied across the full classified-writer set, not just the reported ones — including the highest-volume write in the system (`insert_turn`, one row per turn of every ingested session) and `upsert_project.git_remote` (a git URL can embed credentials the same way `endpoint` did). Non-text writers (identifiers/integers only, e.g. `set_watermark`, `start_run`) are separately classified and bind no free text at all, so `redact_text()` does not apply to them. The following columns are a deliberate, positively-asserted exception within redacting writers — redacting them would break correlation for exactly the rows whose value matched a secret pattern: `projects.path`; `sessions.source_id`/`sessions.native_id`; `router_requests.source_id`/`router_requests.native_id`; `turns.content_fingerprint`; `agent_calls.input_fingerprint`; `skill_pins.content_sha256`. | **IMPLEMENTED** — `src/dreamy/redact.py::redact_text`, applied across classified writers in `src/dreamy/store.py`; see the row below for the regression guard against a future unclassified writer |
| A new `Store` writer method is added later and never gets classified for redaction, so the boundary above silently regresses one writer at a time | Information Disclosure | `tests/integration/test_store_write_boundary.py` provides an automated guard backed by explicit classification sets (`REDACTING_WRITERS`, `NON_TEXT_WRITERS`), not a coverage claim independent of maintaining them: `test_every_writer_is_classified` AST-parses `Store`'s source, finds every non-underscore method whose body contains an inline `"INSERT "`/`"UPDATE "`/`"DELETE FROM"` literal, and fails if that method is in neither set — so a new writer fails CI until someone classifies it, rather than shipping silently unredacted. `test_every_redacting_writer_is_actually_exercised` then fails if a writer is added to the redacting set but never actually driven with a canary, closing the gap between "classified" and "covered." Combined with `test_no_writer_persists_a_secret_in_any_column`, `test_redaction_actually_ran` (a positive control against vacuous absence), `test_database_file_contains_no_secret` (scans raw DB+WAL bytes), and `test_hostile_types_do_not_crash_ingestion` (a hostile type neither crashes ingestion nor bypasses redaction into a BLOB). **This guard has a known, honestly-stated hole**: the AST scan is a literal-SQL heuristic and would miss a writer whose SQL is assembled from variables or built in a helper function rather than written as an inline string — a future refactor toward query-builder or ORM-style SQL construction would silently exit this guard's coverage. | **IMPLEMENTED** — verified by `tests/integration/test_store_write_boundary.py`. Known limitation: literal-inline-SQL detection only, does not cover programmatically-constructed SQL |

## 3. Dreamy's own tools (the agent execution surface)

**Asset:** `agent_sdk.py`, the single module through which `claude_agent_sdk`
is imported, and the four role modules under `agents/` (`research`, `teacher`,
`friends`, `compiler`) that call it.

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| Dreamy's own code directly writes to a target repository, via a filesystem call `src/dreamy/` itself constructs | Tampering, Elevation of Privilege | Verified by inspection: no module under `src/dreamy/` opens a target repository for writing, with any built-in `open()`/`Path.write_*`/`shutil` call. This is a claim about Dreamy's own non-agent code paths only. | **IMPLEMENTED** — structural: no such call is ever constructed in the codebase outside the agent layer covered by the next row |
| A model-issued tool call, via the agent layer, writes to a target repository — a distinct and separate claim from the row above | Tampering, Elevation of Privilege | Every `ClaudeAgentOptions(...)` construction in the codebase sets **none** of `allowed_tools`, `disallowed_tools`, `permission_mode`, `can_use_tool`, or `hooks` — `call_claude` passes only `model`, `env`, `cwd`, `max_budget_usd`, `cli_path`. What effective tool set the CLI grants under those omissions has not been measured, so whether a model-issued write attempt would succeed or be denied is unknown, not assumed safe. | **SPECIFIED** — see the next row and `PERMISSION-POLICY.md` §"Current implementation status" for the target configuration and why it is not yet wired in |
| A future or as-yet-unwritten code path attempts a write, with no permission configuration in place to block it | Tampering, Elevation of Privilege | `call_claude` does not currently set `allowed_tools`, `disallowed_tools`, or `permission_mode` at all (`ClaudeAgentOptions(model=..., env=..., cwd=..., max_budget_usd=..., cli_path=...)`, `src/dreamy/agent_sdk.py`). The inspection-based posture above has no permission-layer backstop today. This is the single most important gap this threat model records. | **SPECIFIED** — see `PERMISSION-POLICY.md` §"Current implementation status" for the target configuration and why it is not yet wired in |
| Spend on paid model calls exceeds the operator's configured cap | Denial of Service (financial) | Pre-call cost-aware gate skips a call it can prove would exceed the cap; per-subprocess `max_budget_usd` clamped to remaining budget as a second layer; post-call ledger re-read marks any overshoot `cap_exceeded_post` rather than hiding it | **IMPLEMENTED** — see §8 for the accuracy caveat on `max_budget_usd` |
| An agent call is made without a resolvable `claude` CLI binary or without router auth, silently proceeding as if it succeeded | Repudiation | Both paths return an explicit `AgentResult` with `stop_reason` set to `"unavailable"` or `"error"` and `error_text` populated; callers check `result.error_text` before persisting anything | **IMPLEMENTED** — `src/dreamy/agent_sdk.py::call_claude` |
| A schema-invalid structured output is persisted as if valid | Tampering | Schema mismatch drops the structured output and marks the row `schema_invalid`; cost is retained (money was spent) but no fabricated data reaches the store | **IMPLEMENTED** — `src/dreamy/agent_sdk.py::_validate_structured` |

## 4. Managed target repositories

**Asset:** The git checkouts the ingested sessions were about — `SAFE-001`'s subject.

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| Dreamy mutates a managed repository (file write, git plumbing that touches `.git/`, worktree creation) | Tampering | ADR-004 (Proposal-only writes, no escape hatch): no `--apply` flag exists in any mode. Read-only git evidence gathering (`changes.py::_run_git`) sets `GIT_OPTIONAL_LOCKS=0`, because `git status` without it opportunistically refreshes the index — itself a write to `.git/` that would violate byte-identity. Only `status --porcelain`, `log`, and `diff --stat` are invoked; no subcommand that mutates state is ever constructed. | **IMPLEMENTED** — `src/dreamy/changes.py`; verified by inspection that no `checkout`, `commit`, `add`, `reset`, `worktree add`, or similar subcommand string appears anywhere in the codebase |
| No `--apply` flag or TUI apply action exists today | Tampering, Elevation of Privilege | Verified by inspection of `cli.py`'s subcommand list (`run`, `install`, `uninstall`, `status`, `skills`, `tui`, `verify-run`) and every TUI view under `tui/views/`: none constructs a write-capable action. | **IMPLEMENTED** |
| A future contributor could still add such a flag or action, since no automated check would catch it | Tampering, Elevation of Privilege | ADR-004 records the no-escape-hatch design as a deliberately absolute, one-assertion-testable invariant, and rejects both an auto-apply-to-worktree option and a one-click-apply option specifically because either would require patch-application machinery to exist in the codebase — "the strongest form of the guarantee is that the code is not there." | **SPECIFIED** — enforcement today is code-review discipline only; a grep-based CI check for write-capable calls outside the output directory, which would make a regression fail the build automatically, does not yet exist |
| The model, via a subagent, is granted write tools that escape the parent session's own read-only configuration | Elevation of Privilege | See §5 — this is the subagent escalation threat, the highest-severity item in this document | **SPECIFIED** |

## 5. Subagent privilege escalation — highest-severity item

**Asset:** Any `AgentDefinition` Dreamy might configure for a subagent, and
the parent session's assumed safety boundary.

A prior review of this codebase recorded subagents as "optional, not
load-bearing" and assumed a subagent could only *tighten* the parent's
permissions. That assumption was wrong, and the correction is the reason this
section exists.

| Threat | STRIDE | Detail | Mitigation | Status |
|---|---|---|---|---|
| A subagent's `AgentDefinition.permissionMode` escalates past the parent's mode | Elevation of Privilege | Per the SDK's permissions documentation, a subagent may declare `bypassPermissions` while the parent runs `default`, `dontAsk`, or `plan`. Only a parent already running `bypassPermissions`, `acceptEdits`, or `auto` blocks a per-subagent override. **A safe parent session does not bound its children.** The installed `AgentDefinition` dataclass corroborates the field exists (`permissionMode: Optional[Literal['default','acceptEdits','plan','bypassPermissions',...]]`) — that is schema evidence only, not proof of the runtime inheritance behavior, which comes from the documentation and has not been exercised in this environment. | Every `AgentDefinition` Dreamy ships must **never set `permissionMode`** at all, and must set `tools` to an explicit narrow allowlist (or `[]`). | **SPECIFIED** — no subagent is defined anywhere in the current codebase (`agent_sdk.py` calls `query()` directly with no `AgentDefinition`), so this constraint is currently satisfied by absence, not by an enforced check. It becomes load-bearing the moment subagents are introduced. |
| A subagent's inline `mcpServers` dict grants tools the parent never registered | Elevation of Privilege | The `dict` branch of the `AgentDefinition.mcpServers` field is an *inline server definition*, not a reference to a parent-registered server — independent of whatever `tools` / `disallowedTools` ceiling the parent enforces on built-in tools. This means a subagent can be handed capability the parent's own tool allowlist never granted. | Every `AgentDefinition` Dreamy ships must **never set `mcpServers`**. | **SPECIFIED**, same absence-based status as above |
| `strict_mcp_config` is assumed to filter subagent-level inline `mcpServers` entries, but this has not been confirmed | Elevation of Privilege | Marked `[INFERENCE]` in `SDK-CAPABILITY-MATRIX.md` — not confirmed from documentation text. | The specified stance, until the underlying SDK behavior is confirmed: assume `strict_mcp_config` does **not** filter subagent-level inline `mcpServers`, and design every constraint above without relying on it. | **SPECIFIED** — the underlying SDK question itself remains an open research gap, tracked in `SDK-CAPABILITY-MATRIX.md`; the stance Dreamy takes in the meantime (assume no protection) is what's specified here |
| Reliance on SDK-level permission inheritance as the sole boundary | Elevation of Privilege | The correct model is defense in depth, not inheritance: OS-level sandboxing and managed settings form the outer boundary; a `PreToolUse` hook is the backstop because hooks run before deny rules and apply even under `bypassPermissions`. | Every layer below must independently assume the SDK-level permission model can be escalated around. | **SPECIFIED** — the hook backstop is not yet wired into `agent_sdk.py`; see `PERMISSION-POLICY.md` |

**Consequence for `SAFE-001`:** Dreamy's mutation-incapability cannot rest on
subagent permission inheritance, because inheritance does not reliably hold.
It must rest on (a) never granting a subagent write-capable tools or
permission escalation in the first place, and (b) the structural fact,
verified today, that no module in the codebase constructs a call capable of
writing to a target repository. Both a conformance test exercising the
escalation paths above and a `PreToolUse` hook backstop are required before
Dreamy ships subagents at all (`SDK-CAPABILITY-MATRIX.md` row 11).

## 6. `ToolAnnotations` are not enforcement

**Asset:** Any future custom tool Dreamy might register (none exist in this
build; `SDK-CAPABILITY-MATRIX.md` disposition #10, "Python custom tools",
is **required** for a future phase but not yet implemented).

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| A tool is trusted to be read-only because it declares `readOnlyHint=True` | Tampering | The SDK reference states plainly that annotations are optional hints and that clients should not rely on them for security decisions. A tool declaring `readOnlyHint=True` can still write to disk — the annotation describes intent to the model and constrains nothing at runtime. | **SPECIFIED** — the actual boundary must be the tool's own implementation, path canonicalization, and permission configuration, never the annotation. No custom tool exists yet to violate this, so there is nothing to verify against today. |

## 7. Redaction and transcript persistence timing

**Asset:** The moment between a model producing output and that output being
written to Dreamy's own local database — the exact seam `SAFE-002` depends on.

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| An unredacted secret in model output reaches Dreamy's local transcript store before any hook has a chance to strip it | Information Disclosure | Reviewed against all 10 Python hook events this SDK build accepts (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `SubagentStart`, `PreCompact`, `Notification`, `PermissionRequest`). **No documented hook fires before local transcript persistence.** `PostToolUse.updatedToolOutput` changes only what the model subsequently sees, not what was already written to disk. This means redaction cannot be delegated to the SDK's hook system for anything the SDK itself persists. | **SPECIFIED** — this constraint cannot be resolved at the SDK layer; Dreamy's own `redact()` call at its own persistence boundary (`agents/*.py` → `store.py`) is the only place it can be enforced, and it is enforced there for content Dreamy itself writes (§2). This document does not claim coverage of any transcript persistence performed internally by the SDK/CLI subprocess outside Dreamy's own store — that surface is out of scope for `SAFE-002` as currently defined, not silently assumed covered. |

## 8. Spend-cap overshoot

**Asset:** The operator's configured `spend_cap_usd`.

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| `max_budget_usd` is checked after spend occurs, against a client-side price-table estimate, not a pre-flight hard stop, so a single call can overshoot the cap | Denial of Service (financial) | Three layers, not one: (1) a pre-call gate skips a call it can *prove* would push the run past the cap, based on the highest per-call cost observed so far for this run; (2) the per-subprocess `max_budget_usd` is clamped to `remaining` as a last defense against one unexpectedly expensive call; (3) a post-call ledger re-read marks any actual overshoot `cap_exceeded_post` and surfaces it in `result.error_text`, rather than hiding it or discarding the (already-paid-for) completed work. | **IMPLEMENTED** — `src/dreamy/agent_sdk.py::call_claude`, contracts 2 and 3 in the inline comments |
| Subagent-halting enforcement of the budget is only guaranteed on CLI ≥ 2.1.217 | Denial of Service (financial) | The installed CLI is `2.1.220`, above the threshold. Pinned exactly; a downgrade would silently reintroduce the gap. | **IMPLEMENTED** as a version constraint — `docs/research/SDK-VERSION-LOCK.md` |
| An operator reads `max_budget_usd` as an exact hard ceiling | Repudiation (of the guarantee's actual strength) | Documented explicitly as best-effort, not exact, in `SDK-CAPABILITY-MATRIX.md` row 14. This threat model repeats that framing rather than allowing the stronger claim to propagate into user-facing docs. | **SPECIFIED** (a documentation discipline, not a code change) |

## 9. Acceptance manifests and harness-store integrity

**Asset (path):** Any filesystem path Dreamy resolves from data it does not
control — a harness project-directory name, or an acceptance-manifest-declared
artifact path. **This section previously documented a real, unmitigated
finding** — an unresolved-tail `..` gap in `pathdecode.py` — which has
since been fixed and is re-verified below by direct execution against the
original repro cases, alongside controls that were already correctly
implemented (the acceptance-manifest symlink handling). Kept as separate
rows deliberately, so a reader does not conflate the two distinct code
paths just because both now pass.

**Asset (provenance):** The `commit` and `generated_utc` fields an acceptance
manifest declares — the only binding between an evidence run and the source
state it claims to have tested, and the only claim about when the run
happened. A prior review found the validator did not read either field
despite both being schema-required, so manifests omitting `commit` entirely,
carrying `commit: "xyz"`, or omitting `generated_utc` were all admitted with
every artifact hash correctly verified — well-formed hashes over a
source-state claim that was never checked.

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| A crafted or malicious harness project-directory name decodes to a path outside the expected filesystem region via slash-boundary guessing on a real, hyphenated directory name | Tampering | `pathdecode.py` never blindly joins components for the *matched* portion of the walk; it walks the filesystem from a fixed root (`/` for Claude Code, the user's home for OMP/Pi) and matches only against directories that actually exist on disk at each level. | **IMPLEMENTED** — `src/dreamy/pathdecode.py`, the matched-prefix walk; see the row below for the unresolved-tail case |
| A crafted directory name whose *unresolved tail* is (or contains) `..` could decode to a path literally containing `..`, which downstream code (`changes.py::gather_git_evidence`, called with this value as `cwd`; `Path(project_path).is_dir()` in `run.py`) would use without canonicalizing or rejecting it | Tampering, Elevation of Privilege | `_decode_under`'s unresolved-tail branch raises `ValueError` on a leaf equal to `.`/`..` or containing `/`/`\`, and `_is_unsafe_cwd()` independently rejects any resulting cwd string containing a `..` segment, a non-`/`-root, or embedded NUL/backslash — two independent layers, not one relying on the other. Covered by `tests/unit/test_pathdecode_traversal.py` and the connector-level `tests/integration/test_connector_hostile_dirs.py`. | **IMPLEMENTED** — `src/dreamy/pathdecode.py::_decode_under`, `::_is_unsafe_cwd`; see §1 for the connector-integration coverage |
| A symlinked run root in an acceptance manifest points outside `~/.local/share/dreamy/acceptance-runs` | Tampering, Elevation of Privilege | The declared root is resolved (`Path(root_raw).expanduser().resolve()`) and containment is checked against the resolved, symlink-followed acceptance-runs base — so a symlinked *root directory* cannot escape, because resolution happens on both sides of the comparison before the containment check. `ACCEPTANCE_RUNS_BASE` itself is a `HOME`-derived configuration value, not a value independently pinned outside the validating process's own environment — but this is not a distinct threat: the manifest cannot set `HOME`, and whoever does control the verifying process's environment already controls the verification outcome directly, with no need to relocate the base. `cli.py`'s comment ("prevents [a claimant] from relocating the trust boundary") slightly overstates what omitting `--runs-base` accomplishes — it is normal configuration behavior, not a defended-against attack — but no exploitable gap follows from it. | **IMPLEMENTED** — `src/dreamy/acceptance.py::validate_run_manifest`, containment check against `base.parents` / `root.parents` |
| A symlinked *artifact* inside an otherwise-valid run root points at a file outside the run, letting a forged hash be computed over out-of-tree content | Tampering | Symlink rejection happens **before** `.resolve()` is called on the artifact path, by walking each path component from the run root and checking `Path.is_symlink()` at every step. This ordering is deliberate and load-bearing: calling `.resolve()` first would silently follow the link, hand downstream code a plain resolved path indistinguishable from a non-symlinked one, and let the SHA-256 be computed over a file the manifest never legitimately referenced. A parents-based containment check alone is insufficient here too — it misses a symlinked directory that is itself a direct child of the run root, because that directory's own parent *is* the root and would pass a naive parents check. The implementation walks components explicitly for this reason. | **IMPLEMENTED** — `src/dreamy/acceptance.py`, artifact-resolution loop (`walked = walked / part; if walked.is_symlink(): raise ...`, followed only then by `target = unresolved.resolve()`) |
| An artifact path is declared absolute, bypassing the run-root-relative containment model entirely | Tampering | Rejected outright before any resolution is attempted: `if Path(rel).is_absolute(): raise ManifestError(...)`. | **IMPLEMENTED** — `src/dreamy/acceptance.py` |
| A cited evidence path elsewhere in the pipeline (e.g. coldstart's prompt-artifact citation check) is compared for containment without first resolving both sides, permitting a symlink to spoof "inside the evidence root" | Tampering | `_is_prerequisite_created` in the coldstart module resolves both the cited path and the evidence root before comparing, and the comparison is scoped to *that artifact's own* evidence root rather than any path containing `/evidence/` anywhere — a global substring rule would readmit paths belonging to other projects' trees entirely. | **IMPLEMENTED** — `src/dreamy/coldstart.py::_is_prerequisite_created`, `_artifact_evidence_root` |
| An acceptance manifest omits `commit` entirely, declares a syntactically-invalid one (`"xyz"`), or fabricates a well-formed but nonexistent 40-hex SHA — making the run unfalsifiable, since the artifacts prove something ran but nothing says against what source state | Tampering, Repudiation | `commit` is required and constrained to `_COMMIT_RE` (a full 40-character lowercase-hex string), then independently checked for existence as a real commit object in the target repository via `_commit_exists`. This is existence, deliberately not equality with `HEAD` — evidence is produced at one commit and validated later, often much later, so requiring `commit == HEAD` would reject every historical run the moment anything else was committed. It does **not** establish that the recorded commit actually produced the artifacts, or that it matches the current checkout — nothing short of signed provenance could establish that. It establishes only that the claimed source state is one this repository can produce, which is enough to reject an absent, malformed, or fabricated SHA and to make the claim auditable by someone who can check out that commit. | **IMPLEMENTED** — `src/dreamy/acceptance.py::_commit_exists`, `validate_run_manifest` |
| An acceptance manifest omits `generated_utc`, or declares it in a form other than zero-offset UTC (naive/timezone-less, or a non-zero-offset instant such as `+05:00`) | Tampering | `generated_utc` is required to be a non-empty ISO-8601 string that `datetime.fromisoformat` can parse, and `parsed_utc.utcoffset()` must equal exactly `timedelta(0)` — this constrains the value to zero UTC offset, it does not rewrite or normalize it: a `+05:00` offset denotes an exact instant unambiguously, it is simply rejected for not carrying the zero offset the field name promises (both `Z` and `+00:00` suffixes pass, since neither changes the offset). A naive (timezone-less) datetime is the one case that is genuinely ambiguous, since `utcoffset()` returns `None` for it. | **IMPLEMENTED** — `src/dreamy/acceptance.py::validate_run_manifest` |

## 10. MCP servers

**Asset:** None exist in this build. Disposition #9 in `SDK-CAPABILITY-MATRIX.md`
marks external and in-process MCP servers **required** to host Dreamy's
planned eight narrow read-only tools — but zero `create_sdk_mcp_server` or
`@tool` call sites exist anywhere in the current source tree.

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| A registered MCP server exposes a capability broader than the "eight narrow read-only tools" design intent | Elevation of Privilege | Every MCP tool Dreamy defines, once built, must be typed, bounded, and redacted — replacing raw filesystem access, not adding to it (`SDK-CAPABILITY-MATRIX.md` disposition #10 rationale). `strict_mcp_config` should be set once servers exist, though its exact scope against subagent-level inline definitions is unresolved (§5). | **SPECIFIED** — nothing to violate yet, because nothing is built yet. This row exists so the constraint is on record before the first tool is written, not retrofitted after. |

## 11. Plugin loading — prohibited

**Asset:** `~/.claude/plugins/` — currently touched only for skill-pin
lookup (`skill_pins.py::_skill_path` globs the plugin cache for a
`SKILL.md` body to hash and pin), never for loading executable plugin code
into an agent session.

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| Unpinned, unreviewed third-party plugin code executes inside the same trust boundary as Dreamy's own agent calls | Elevation of Privilege, Tampering | Disposition #16 in `SDK-CAPABILITY-MATRIX.md` marks `plugins` / `SdkPluginConfig` **prohibited** outright: "unpinned third-party code inside the trust boundary." `agent_sdk.py::call_claude` never constructs a `ClaudeAgentOptions` with a `plugins` argument. | **IMPLEMENTED** — verified by inspection: no `plugins=` keyword appears in any `ClaudeAgentOptions(...)` construction in the codebase |
| Skill *content* pinned from a plugin cache is trusted implicitly because it was pinned once | Tampering | Skill bodies are content-addressed: `skill_pins.py::pin_skill` SHA-256-hashes the body at pin time and `drift_check` compares the currently-read body's hash against the stored pin, so a subsequently-modified skill file is detectable as drift rather than silently re-inlined. This is provenance tracking, not code execution — pinned skill *text* is inlined into a compiled prompt artifact; it is never executed. | **IMPLEMENTED** — `src/dreamy/skill_pins.py` |

## 12. Prompts sent to the model

**Asset:** The prompt text `agent_sdk.py::call_claude` sends to the router.

| Threat | STRIDE | Mitigation | Status |
|---|---|---|---|
| A prompt assembled from harness content carries an unredacted secret to the model provider | Information Disclosure | Prompts are built from `content_excerpt` fields, which are redacted at ingestion (§1, §2) before they ever reach a role module's prompt-assembly step. | **IMPLEMENTED** — inheriting the same scope limits as §2/§7 |
| A prompt injects instructions that cause the model to attempt an out-of-scope tool call | Elevation of Privilege | Not a prompt-level defense per `SAFE-003` — addressed structurally, not by prompt hygiene. | **SPECIFIED** — see §13 |

## 13. `SAFE-003` — safety enforcement must not depend on prompts alone

This requirement is cross-cutting rather than asset-specific, so it is
called out on its own. Every mitigation above that could plausibly be
mistaken for "the system prompt tells the model not to write files" is
explicitly **not** that:

- The read-only posture for target repositories (§4) rests on the absence of
  write-capable code paths, not on an instruction to the model. That is
  stronger than a prompt boundary but weaker than an enforced one: the
  permission-layer backstop that would make it enforced is **SPECIFIED**,
  not yet implemented — see row 71 above and `PERMISSION-POLICY.md`.
- The plugin prohibition (§11) rests on `agent_sdk.py` never constructing a
  `plugins=` option, not on a policy statement.
- The spend cap (§8) is enforced by pre-call and post-call ledger checks in
  Python, not by asking the model to stay under budget.
- Where a prompt-only boundary is currently the *only* thing in place — most
  notably, the absence of a `PreToolUse` hook backstop and the absence of an
  explicit `allowed_tools`/`disallowed_tools`/`permission_mode`
  configuration on `call_claude`'s `ClaudeAgentOptions` (§3) — this document
  records that as an open gap against `SAFE-003`, not as a satisfied
  requirement. See `PERMISSION-POLICY.md` for the concrete remediation.

## Summary — implemented vs. specified

| # | Item | Status |
|---|---|---|
| 1 | Transcript parsing is defensive against malformed input; the matched-prefix portion of project-path decoding resists slash-boundary guessing | IMPLEMENTED |
| 1b | Project-path decoding rejects an unresolved `..` tail rather than returning an escaping path | IMPLEMENTED — see §1 and §9 |
| 1c | The OMP connector integrates the decoder's traversal-rejection without regressing graceful per-file degradation | IMPLEMENTED — `omp_pi.py` and `claude.py` both catch the decoder's `ValueError`; see §1 |
| 2 | Pattern-based secret redaction | IMPLEMENTED |
| 3 | Known-value secret redaction (closes the D11 gap) | IMPLEMENTED |
| 4 | Redaction across the whole `Store` write boundary, for every classified redacting writer's caller-supplied free text | IMPLEMENTED — `src/dreamy/redact.py::redact_text`, applied to classified free-text bindings throughout `src/dreamy/store.py`; see §2 |
| 4b | Automated regression guard against a future unclassified `Store` writer shipping unredacted | IMPLEMENTED — `tests/integration/test_store_write_boundary.py`; known limitation: literal-inline-SQL heuristic only, see §2 |
| 5 | Redaction guaranteed before *any* SDK-side transcript persistence, not just Dreamy's own store | SPECIFIED — out of scope for Dreamy's own control at the SDK layer; see §7 |
| 6 | No module writes to a target repository | IMPLEMENTED |
| 7 | `allowed_tools` / `disallowed_tools` / `permission_mode` configured on agent calls | SPECIFIED — `call_claude` sets none of them today |
| 8 | Subagent `permissionMode` / `mcpServers` never set | SPECIFIED — currently true by absence only, no subagent exists yet |
| 9 | `PreToolUse` hook backstop | SPECIFIED — not yet wired |
| 10 | Spend-cap pre-call gate, subprocess clamp, post-call overshoot marking | IMPLEMENTED |
| 11 | Symlink rejection before resolve() in acceptance-manifest artifact validation (`acceptance.py`) | IMPLEMENTED |
| 11b | Traversal rejection in project-path decoding (`pathdecode.py`) — a distinct code path from #11, not covered by it | IMPLEMENTED — see #1b |
| 12 | Run-root containment via resolved-path check | IMPLEMENTED |
| 12b | Acceptance-manifest `commit` required, shape-constrained, and checked for existence as a real commit object | IMPLEMENTED — see §9, provenance asset |
| 12c | Acceptance-manifest `generated_utc` required and constrained to zero UTC offset | IMPLEMENTED — see §9, provenance asset |
| 13 | Plugin loading disabled | IMPLEMENTED — no `plugins=` construction anywhere |
| 14 | Skill content pin/drift detection | IMPLEMENTED |
| 15 | MCP tools bounded and typed | SPECIFIED — no MCP tools exist yet to bound |
| 16 | `ToolAnnotations` never relied on for enforcement | SPECIFIED — a design rule with nothing to violate yet |
| 17 | No `--apply` flag or TUI write action exists today | IMPLEMENTED |
| 18 | CI-enforced regression check blocking a future write-capable addition | SPECIFIED — not yet built |

One item remains open at highest priority:

- **#7** — Dreamy's read-only guarantee for `SAFE-001` currently depends
  entirely on the code never *attempting* a write, with no permission
  configuration actively denying one if it did. Addressed concretely in
  `PERMISSION-POLICY.md`.

Path-traversal decoding and its connector integration (items #1b, #1c,
#11b) are covered by `tests/unit/test_pathdecode_traversal.py` and
`tests/integration/test_connector_hostile_dirs.py`.
