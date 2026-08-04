# ARCHITECTURE

Requirements: `PKG-001`, `PKG-002`, `SAFE-001`, `SAFE-002`, `SAFE-003`

Describes the system as it exists at `phase/00-foundation`. Module names, line
counts, and dependency edges below were extracted from the source tree by AST
walk, not written from memory. 51 modules, 10,420 lines under `src/dreamy/`
(measured 2026-08-01; re-measure rather than trusting this figure).

## What Dreamy is

A read-only reconciler for coding sessions spread across six agent harnesses
(OMP, Pi, Claude Code, Codex, OpenCode, Nine Router). It ingests a trailing
window from each harness's own store, correlates sessions into per-project
timelines, and compiles evidence-cited prompts.

It reads other tools' data. It never writes to the repositories that data
describes.

## Trust boundaries

Three boundaries, in decreasing order of trust:

```
┌─ Dreamy's own state ────────────────────────────────┐  read-write
│  ~/.local/share/dreamy/{state.db, acceptance-runs}  │
└─────────────────────────────────────────────────────┘
┌─ Harness stores ────────────────────────────────────┐  read-only
│  ~/.claude/projects, ~/.codex, ~/.omp, …            │
└─────────────────────────────────────────────────────┘
┌─ Managed target repositories ───────────────────────┐  read-only,
│  the git checkouts the sessions were about          │  git plumbing only
└─────────────────────────────────────────────────────┘
```

`SAFE-001` requires managed repositories stay byte-identical. **It is currently
UNVERIFIED.** No module under `src/dreamy/` opens a target repository for
writing, which establishes that Dreamy never *requests* write capability — but
the agent layer configures no `allowed_tools`, `disallowed_tools`, or
`permission_mode` at all, so nothing has been shown to *refuse* a write if the
model attempted one. See `docs/security/PERMISSION-POLICY.md` for the
configuration that would close this, and
`tests/sdk_conformance/test_mutation_denial.py` for the opt-in test that would
evidence it.

## Layers

Dependency direction is inward. Nothing in a lower layer imports a higher one.

| Layer | Modules | Role |
|---|---|---|
| Entry | `__main__`, `cli` | Argument parsing, dispatch |
| Orchestration | `run`, `ingest`, `coldstart` | Pipeline sequencing |
| Analysis | `analyze`, `correlate`, `changes`, `efficacy`, `evidence_projection` | Deriving findings from ingested data |
| Agent | `agent_sdk`, `agents/*`, `prompt_compiler`, `skill_pins` | SDK-backed reasoning; isolated behind one module |
| Connectors | `connectors/{claude,codex,omp_pi,opencode,router}` | Per-harness readers, one per source |
| Storage | `store`, `read`, `retention`, `project` | SQLite persistence and query |
| Safety | `redact`, `pathdecode`, `protocol` | Boundary enforcement, shared contracts |
| Acceptance | `acceptance` | Run-manifest validation |
| Presentation | `tui/*`, `reports`, `bundle`, `diff` | Display and export |

### Largest modules

`run.py` (912) sequences the pipeline. `correlate.py` (707) does session
matching. `evidence_projection.py` (648) and `store.py` (636) carry the data
model. Their size reflects genuine domain complexity, not accumulated drift, but
`run.py` is the module most likely to need decomposition next.

## Dependency rules

1. **Connectors never import each other.** Each reads one harness. Adding a
   seventh harness touches `connectors/` and nothing else. Verified: every
   connector imports only `protocol`, `redact`, `store`, and `pathdecode`.
2. **The SDK is confined to one module.** An AST scan for `claude_agent_sdk`
   across all 51 modules returns exactly one hit: `agent_sdk.py`. No connector,
   storage, safety, or presentation module touches it.
3. **Safety modules import nothing internal.** `redact`, `pathdecode`, and
   `protocol` have zero internal dependencies, so they cannot be subverted by a
   cycle through a higher layer.
4. **`acceptance.py` is imported by `cli`** — see `## The acceptance validator`.

## Degraded operation without the SDK

`SDK-*` requirements are satisfied by the `agent` extra. With that extra absent,
these must continue to work:

- ingestion from all six connectors
- correlation and timeline construction
- git evidence collection
- deterministic prompt compilation
- reporting and export
- every safety behaviour

Only agent-backed analysis degrades. `agent_sdk.py` is the single import site, so
its absence is a contained failure rather than an import error at startup.

## The acceptance validator

`acceptance.py` validates run manifests that claim a gate. It exists as product
code, not test code, because the CLI, the evidence gate, and the citation
self-check all need one answer to "which evidence tree may this gate read, and is
it trustworthy?"

It had **zero production importers** for part of this phase — its only callers
were tests, and the gate test skips unless an environment variable is set. A
green suite was therefore compatible with the validator never running. It is now
reachable as `dreamy verify-run`, with a test asserting a production importer
exists.

Admission is deliberately *not* gated on the gate matrix's own status cell:
reading it was circular, since the cell records the conclusion. The matrix
supplies the gate's requirement-ID set; the status is a derived record.

## Packaging

`src/` layout (`PKG-001`), hatchling backend, console entry point
`dreamy = "dreamy.cli:main"`. The layout choice is load-bearing rather than
stylistic: under a flat layout, running from the repository root imports the
working copy instead of the installed package. Verified against a purpose-built
flat-layout counterexample — see `docs/acceptance/GATE-MATRIX.md` G2.

Runtime dependencies are empty by design. The TUI ships under a `tui` extra with
`textual` pinned; the SDK under an `agent` extra with `claude-agent-sdk` pinned
and hash-locked.
