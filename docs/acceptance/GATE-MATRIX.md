# GATE-MATRIX

**Superseded by the addendum completion gate (G1-G8), evaluated at
`evidence/completion-gate/VERDICT.txt`.** This matrix's own gate rows
(G0-G13) predate the `SPEC-addendum-prompts-p1p2-tui.md` scope and are
retained for forensic value (see the G2 postmortem below on false
evidence capture, still instructive) but are NOT actively maintained
going forward.

Requirement → command → evidence, for every acceptance gate.

Gate definitions (G0–G13) and requirement IDs are transcribed from the product
specification. **Nothing here is invented**: a gate whose evidence does not
exist is recorded `BLOCKED`, never assumed or forecast.

Status vocabulary — deliberately small, so a partial result cannot masquerade
as a pass:

| Status | Meaning |
|---|---|
| `PASS` | Command run, evidence captured, criteria met |
| `FAIL` | Command run, criteria not met |
| `BLOCKED` | Cannot be evaluated — prerequisite absent, dependency uninstalled, or evidence never produced |

There is no partial-credit status. `BLOCKED` is not a soft pass.

Repo state for this matrix: branch `phase/00-foundation`, commit
`7a67b087960115a4772a90fb67ff61fcb0620641`, baseline `15001fd`.

## Gate status

Every requirement ID in the Req IDs column is defined normatively in
`docs/specifications/SPEC-DREAMY-V2.md`. A gate whose brief-named identifier has
no normative definition yet shows UNASSIGNED rather than citing an ID that
nothing defines; those gates are BLOCKED on the specification itself.

Evidence paths use placeholders (`$XDG_DATA_HOME`, `$DREAMY_ACCEPTANCE_EVIDENCE`)
because this document is version-controlled and must not pin a developer's home
directory. The run manifest — which is NOT committed — stores the resolved
absolute paths, so references stay machine-resolvable without leaking a checkout
path into git.


| Gate | Req IDs | Requirement | Command | Status | Evidence |
|---|---|---|---|---|---|
| G0 | SPEC-001 | Specification, ADRs, threat model, requirement matrix approved | — | **BLOCKED** | All 20 required documents now exist (verified 2026-08-01 by presence check over the brief's list): `SPEC-DREAMY-V2.md` (33 requirement IDs, bidirectional with this matrix), `BUILD-FROM-ZERO.md`, `ARCHITECTURE.md`, `AGENT-PIPELINE.md`, `OBSERVABILITY.md`, `SDK-CAPABILITY-MATRIX.md`, `SDK-VERSION-LOCK.md`, `THREAT-MODEL.md`, `PERMISSION-POLICY.md`, `RUNBOOK.md`, `RELEASE.md`, `NEW-MACHINE-RESULTS.md`, `ADR-008`…`ADR-011`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`. **Blocked on approval, not authorship** — the gate requires *approved*, and the producer of these documents cannot approve them (RL-3). Needs an independent reviewer |
| G1 | BOOT-001, BOOT-002 | Pristine machine: clone, bootstrap, `dreamy doctor`, tests | `docs/specifications/BUILD-FROM-ZERO.md` | **BLOCKED** | Procedure now written **and executed** — but on the development host against `git archive` trees in `/tmp`, not a pristine machine, so it validates the procedure and not machine independence. Results and both wheel hashes recorded in `NEW-MACHINE-RESULTS.md` (status: NOT RUN). Executing it found and fixed two real defects: `SDK-VERSION-LOCK.md` placed `PIP_USER=0` before `venv` instead of on the `pip` call (fails inside a virtualenv), and the build frontend `build` is absent from `requirements-build.lock` — only the `hatchling` backend is pinned. Still blocked on: no pristine machine; `dreamy doctor` does not exist (registered: `run`, `install`, `uninstall`, `status`, `skills`, `tui`, `verify-run`); `textual` not installed so `dreamy tui` is unexercised |
| G2 | PKG-001, PKG-002, PKG-003 | Wheel **and** editable installation work; `dreamy` runs outside the checkout | `python -m hatchling build -t wheel` ; `pip install dist/dreamy-*.whl` ; `pip install -e <repo>` ; then run `dreamy status` with an explicit foreign `cwd=` (see below) | **BLOCKED** | **Evidence SUPERSEDED.** Run `run-260801-140000-g2-packaging` froze validator `9a7ea3b2…` / wheel `eea81d3e…`; both have since DRIFTED (`SAFE-002` store redaction, `verify-run` CLI wiring, new tests). Reviews on the frozen set: 2 admitting, then 2 FAIL. A later re-capture (`run-260801-180000-phase00-foundation`) destroyed 4 of 12 artifacts and is unrecoverable — see `~/.local/share/dreamy/acceptance-runs/INCIDENT-260801-cwd-contamination.md` (outside the repo; there is also an INCIDENT-260801-cwd-contamination-ERRATUM.md sibling in the same directory). **The previous command column here prescribed `cd / && dreamy status`, which is what produced that false evidence**: tooling that restores its own working directory makes the `cd` a no-op, so the probe never leaves the checkout. Measure with `subprocess.run(..., cwd=<far>)` or `os.chdir()`, printing `pathlib.Path.cwd()` from inside the child. Pinned by `tests/unit/test_foreign_cwd_claims.py`. Needs full evidence regeneration from one coherent state plus a fresh independent review |
| G3 | SDK-001, SDK-002, SDK-003 | Every selected SDK capability has an executable real test | `pytest tests/sdk_conformance` | **BLOCKED** | Dispositions assigned for all 18 capabilities and the assignment *rule* recorded in `ADR-010` (9 required, 4 optional, 2 prohibited, 2 not-applicable, 1 required-but-constrained); passes 1/3/4 complete with 4 recorded open items. `tests/sdk_conformance/` added — 9 contract tests exist in `tests/sdk_conformance/test_installed_contract.py`, but pass status is **UNVERIFIED in the gate environment**: `pytest tests/sdk_conformance -q -rs` yields `3 skipped, 0 passed` because the `agent` extra is absent and `pytest.importorskip` (`tests/sdk_conformance/test_installed_contract.py:20-22`) skips the module wholesale; would be verifiable in an interpreter with `claude-agent-sdk==0.2.128` installed. **Newly quantified: only 4 of 10 `required` capabilities are referenced anywhere in `src/dreamy/`.** Unwired: permissions, hooks, MCP servers, custom tools, subagents, skills / setting sources — so six conformance tests cannot exist yet, and the eight mandated read-only tools named in `AGENT-PIPELINE.md` exist in no module. Table pinned bidirectionally by `tests/unit/test_adr010_capability_claims.py`. Also blocked on `SAFE-001` (`test_mutation_denial.py` is opt-in and currently SKIPPED, so it evidences nothing yet) |
| G4 | INGEST-001 | Real ingestion; repeat scan idempotent; source failures bounded | `DREAMY_RUN_ACCEPTANCE=1 pytest -m acceptance` | **BLOCKED** | Opt-in gated; not run this phase |
| G5 | CORR-001 | All sessions resolve or are explicitly unattributed | `DREAMY_RUN_ACCEPTANCE=1 pytest -m acceptance` | **BLOCKED** | Not exercised this phase |
| G6 | SDK-004, SDK-005, SDK-006, SDK-007, SDK-008, SDK-009, SDK-010 | Agent pipeline: streaming, hooks, permissions, structured output, costs, MCP, subagents | `pytest tests/sdk_conformance` | **BLOCKED** | Depends on G3 dispositions |
| G7 | COMP-001 | Four deterministic cold-start-valid artifacts per project | `dreamy run --lookback-days 30` | **BLOCKED** | Not exercised this phase |
| G8 | OBS-001, OBS-002, OBS-003, OBS-004, OBS-005 | Observability accounts for every run, phase, agent, tool, retry, cost, failure | — | **BLOCKED** | `OBSERVABILITY.md` and `ADR-011` written; event schema specified. Three implementation gaps found while writing, one FIXED (`SAFE-002`: agent output reached SQLite unredacted — redaction now at the `Store.finish_agent_call` boundary, proven by a disk-level test that scans the database file). Blocked on the other two gaps and on the schema being implemented |
| G9 | SAFE-001, SAFE-002, SAFE-003 | Independent VG-S1 and VG-S2 pass with fresh evidence | — | **BLOCKED** | **`SAFE-001` is UNVERIFIED, not merely unexercised.** An AST scan shows Dreamy never requests `Write`/`Edit`/`Bash`, which establishes intent, not enforcement. `tests/sdk_conformance/test_mutation_denial.py` would settle it by requiring `DENIED_BY_PERMISSION` from a real call; it is opt-in (`DREAMY_RUN_SDK_MUTATION_TEST=1`) and SKIPPED, and a skipped test is not evidence |
| G10 | OPS-001 | Manual and launchd runs match; locking, retention, status, uninstall | `dreamy install` ; `dreamy status` ; `dreamy uninstall` | **BLOCKED** | Not exercised this phase |
| G11 | TUI-001 | Six real-data TUI views, streaming, narrow layouts, empty/error states | `dreamy tui` | **BLOCKED** | `textual` **NOT INSTALLED**; `dreamy.tui.*` unimportable. The pinned `textual==8.2.8` in the `tui` extra is a declared test target, never executed |
| G12 | BOOT-001, REL-001 | Second pristine machine reproduces normalized artifacts and package hashes | `tools/verify-locks.sh` (necessary, not sufficient) | **BLOCKED** | Locking invariants hold (exit 0, 4 locks, 638 hashes); no second machine exists |
| G13 | GIT-001, GIT-002, GIT-003, REL-001 | Protected-branch merge, signed tag, GitHub release, checksums | — | **BLOCKED** | No git remote configured; nothing pushed |

**0 PASS, 0 FAIL, 14 BLOCKED.**

### Why G2 is BLOCKED despite passing its install checks

G2 was previously BLOCKED because the suite exited 1 — a failing
`test_all_citations_resolve_or_are_sealed`, which swept the entire historical
evidence tree and could never pass while any past artifact held a dead citation.

That test no longer exists. It was replaced by `test_current_run_citations_resolve`,
scoped to one hash-verified run, plus `test_historical_citation_debt_is_recorded`,
which ratchets the historical debt without gating on it. The suite now exits 0.

The row above was stale for a period after those conditions changed. An
independent review caught it by copying the matrix, flipping only the G2 status
cell, and re-validating a byte-identical manifest: ACCEPTED, exit 0. That
isolated the stale cell as the sole cause of rejection, and showed the earlier
refusal was citing its own bookkeeping as external blockage.

## Supporting checks (not themselves gates)

These are prerequisites, recorded because gates cite them.

| Check | Command | Status | Evidence |
|---|---|---|---|
| Lint | `/tmp/dreamy-g2e/bin/ruff check src tests` | PASS (exit 0) | baseline `15001fd` had 548 findings |
| Types | `/tmp/dreamy-g2e/bin/mypy` | PASS (exit 0, 51 files) | baseline had 18 errors in 10 files |
| Dependency locking | `tools/verify-locks.sh` | PASS (exit 0) | 4 locks, 638 hashes, backend pinned exactly |
| Import health | walk `dreamy.*` excluding tui | PASS | 40/40 |
| Test suite | `/tmp/dreamy-g2e/bin/pytest -q` | PASS (exit 0) | counts re-measured at each run; 2026-08-01: 166 passed, 10 skipped |

Counts are recorded with the date they were measured, not asserted as fixed
values. A hardcoded number is the defect this row previously carried: it read
`FAIL (exit 1) — 19 passed, 1 failed` long after the named test was deleted,
contradicting the paragraph directly above it.

## Closed blocker — citation self-check

`test_all_citations_resolve_or_are_sealed` swept the whole historical evidence
tree and could never pass while any past artifact held a dead citation. It was
replaced by `test_current_run_citations_resolve` (scoped to one hash-verified
run) and `test_historical_citation_debt_is_recorded` (ratchets the debt without
gating on it). Both live in `tests/integration/test_citation_selfcheck.py`.

The historical debt itself is unchanged and still real: 146 unresolved anchors,
113 of them absolute machine-specific paths embedded in reviewer markdown —
dead *and* pinned to one developer's checkout, which the specification forbids.
Detail in `$DREAMY_ACCEPTANCE_EVIDENCE/CITATION-ANCHOR-FINDING.md` (deliberately
outside the repository). Recording it as debt is not the same as fixing it.

## Current-run selection

A current-run manifest validator is required so this gate reads exactly one
declared run rather than sweeping the whole historical tree. It must fail closed
when the manifest is absent, when the declared root escapes the acceptance base,
when a declared artifact's hash does not match, or when this matrix marks the
covered gate `BLOCKED`.

Because 13 of 14 gates are `BLOCKED`, the validator currently has almost nothing
it may legitimately admit. That is the correct outcome, not a defect to engineer
around.

## Maintenance rule

A row moves off `BLOCKED` only when its command has been run and its evidence
captured — never by forecast, and never because a related gate passed.
