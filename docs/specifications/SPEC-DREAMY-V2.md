# SPEC-DREAMY-V2

Normative requirements for Dreamy v2.

## Scope of this document

This is a **partial specification**. It transcribes the normative additions
supplied in the product brief and nothing more. Terminology, schemas, and the
full acceptance criteria named in the brief are **not yet written**; sections
below marked *NOT WRITTEN* are placeholders, not omissions to be inferred
around.

Every requirement carries a stable identifier. Tests, gates, commits, pull
requests, and evidence manifests reference these identifiers.
`docs/acceptance/GATE-MATRIX.md` maps gates to them.

Requirements here are **normative**. A requirement not listed here has no
normative force, and no gate may be satisfied by appeal to one.

## RFC 2119 usage

MUST / MUST NOT / SHOULD / MAY carry their RFC 2119 meanings. `NEVER` is
equivalent to MUST NOT.

## Packaging — PKG

| ID | Requirement |
|---|---|
| PKG-001 | Dreamy MUST use a standard `src/dreamy` package layout. |
| PKG-002 | Production execution MUST NOT depend on the source checkout or developer home-directory paths. |
| PKG-003 | Build and install metadata MUST be declared in `pyproject.toml`. |

## Agent SDK — SDK

| ID | Requirement |
|---|---|
| SDK-001 | The installed Agent SDK API MUST be inventoried and pinned. |
| SDK-002 | Every documented SDK capability MUST receive an explicit disposition. |
| SDK-003 | Every enabled capability MUST have a real conformance test. |
| SDK-004 | Agent tools MUST be default-deny and least-privilege. |
| SDK-005 | Production agents MUST have no target-repository mutation tools. |
| SDK-006 | Agent outputs MUST use versioned schemas and local validation. |
| SDK-007 | Interactive operation MUST support streaming, interruption, cancellation, and human input. |
| SDK-008 | Subagents MUST have bounded depth, turns, time, tools, and cost. |
| SDK-009 | Skills, plugins, prompts, and MCP tools MUST have recorded provenance. |
| SDK-010 | Missing or unhealthy optional capabilities MUST produce explicit degraded states. |

## Observability — OBS

| ID | Requirement |
|---|---|
| OBS-001 | Every run MUST expose a redacted event stream. |
| OBS-002 | Every event MUST be correlated by run, trace, agent, and parent identifiers. |
| OBS-003 | The system MUST expose live and historical orchestration views. |
| OBS-004 | Permission decisions, tools, hooks, retries, costs, todos, and human-input waits MUST be observable. |
| OBS-005 | Dreamy MUST NOT claim to expose or persist hidden chain-of-thought. |

## Delivery — GIT

| ID | Requirement |
|---|---|
| GIT-001 | Every phase MUST be delivered through a pushed branch and reviewed pull request. |
| GIT-002 | Protected history MUST NOT be force-pushed. |
| GIT-003 | Every merge MUST identify requirements, tests, and evidence. |

## Bootstrap — BOOT

| ID | Requirement |
|---|---|
| BOOT-001 | A pristine machine MUST be able to resolve every prerequisite from version-controlled instructions. |
| BOOT-002 | Required skills and plugins MUST be installable from a lock manifest, or fail with an exact remediation. |

## Release — REL

| ID | Requirement |
|---|---|
| REL-001 | Release artifacts MUST include checksums, versions, schema versions, and provenance. |

## Safety — SAFE

| ID | Requirement |
|---|---|
| SAFE-001 | Managed repositories MUST remain byte-identical across the entire run. |
| SAFE-002 | Secrets MUST NOT persist in databases, logs, events, reports, artifacts, or evidence. |
| SAFE-003 | Safety enforcement MUST NOT depend on prompts alone. |

## Gate-derived requirements — SPEC, INGEST, CORR, COMP, OPS, TUI

*(Explanatory, non-normative: these IDs are cited by
`docs/acceptance/GATE-MATRIX.md`, each transcribed from the acceptance outcome
the product brief states for its gate. Only the table rows below are
normative.)*

| ID | Gate | Requirement |
|---|---|---|
| SPEC-001 | G0 | The V2 specification, ADRs, threat model, and requirement matrix MUST exist and be approved. |
| INGEST-001 | G4 | Every present source MUST contribute; a repeat scan MUST be idempotent; source failures MUST be bounded. |
| CORR-001 | G5 | Every session MUST resolve or remain explicitly unattributed; links MUST retain confidence and reasons. |
| COMP-001 | G7 | Four deterministic, cold-start-valid artifacts MUST exist per project. |
| OPS-001 | G10 | Manual and launchd runs MUST match; locking, retention, status, and uninstall MUST work. |
| TUI-001 | G11 | Six real-data views, streaming orchestration, narrow layouts, and empty/error states MUST pass. |

## NOT WRITTEN

The brief requires the following, which do not exist yet. Listed so their
absence is explicit rather than discovered later:

- Terminology and definitions
- Data schemas (event, artifact, structured-agent-output)
- Invariants beyond the SAFE series
- Per-requirement acceptance criteria

Written since this section was first drafted, and no longer absent:
`docs/architecture/{ARCHITECTURE,AGENT-PIPELINE,OBSERVABILITY}.md`,
`docs/security/{THREAT-MODEL,PERMISSION-POLICY}.md`,
`docs/operations/{RUNBOOK,RELEASE}.md`, ADR-011,
`docs/specifications/BUILD-FROM-ZERO.md`, ADR-009 (skill sources), and
ADR-010 (SDK capability disposition). Their existence does not by itself close
G0 — the four content items above remain outstanding, and `SPEC-001` requires
the specification to be *approved*, which the producer of these documents
cannot do (RL-3).

Until those exist and an independent reviewer approves them, G0 remains
`BLOCKED`.
