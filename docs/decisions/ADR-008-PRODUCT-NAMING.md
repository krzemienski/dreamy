# ADR-008 — Product naming migration to `dreamy`

- **Status:** Accepted
- **Date:** 2026-08-01
- **Requirement IDs:** `PKG-001`, `PKG-002`, `PKG-003`
- **Supersedes:** the `continuity` / `cross-harness-continuity` identifier set

## Context

The product shipped under two names. `continuity` described the *problem domain*
— reconciling coding sessions across harnesses — while `dreamy` was the tool.
A single artifact carried both: the package directory, the launchd label, the
database filename, environment variables, CLI help text, and documentation each
picked one, inconsistently.

Two names for one product is not cosmetic. It splits the launchd label an
operator must `bootout`, the state directory a backup must capture, and the
identifier a support request has to quote.

## Decision

`dreamy` is the sole product identifier. `continuity` survives only as a plain
English noun in prose describing what the tool does — never as an identifier.

Concretely:

| Surface | Value |
|---|---|
| Distribution name | `dreamy` |
| Import package | `dreamy` (src layout, `src/dreamy/`) |
| Console entry point | `dreamy = "dreamy.cli:main"` |
| launchd label | `com.nick.dreamy` |
| State directory | `~/.local/share/dreamy/` |
| Acceptance runs | `~/.local/share/dreamy/acceptance-runs/` |
| Environment prefix | `DREAMY_` |

## Consequences

The migration is verified against the **built artifact**, not the source tree.
That distinction mattered: after `src/`, `tests/`, `config/`, `packaging/`, and
`docs/` were all clean, the wheel still shipped one occurrence. `README.md` line
3 read "coding-continuity reconciler", and hatchling hoists the README into
`dist-info/METADATA` — so every install carried the superseded name in the field
`pip show` prints.

Source-tree greps would have reported the migration complete. The check that
caught it scans the `.whl`:

```
files in wheel containing 'continuity': 0
launchd labels shipped: ['com.nick.dreamy']
```

Wheel `d30da79e…`. The check belongs on the artifact because the artifact is what
a user installs.

### Not renamed

Development-tool state (`.mimirs/`, `banks/`, `.claude/`, `.shannon/`) and
historical planning documents under `plans/` retain the old string. They are not
product surfaces and are not shipped. Rewriting a historical planning document to
match a later decision would falsify the record of when that decision was made.

### Follow-on

`SPEC-DREAMY-V2.md` carries the requirement IDs this ADR satisfies. Gate G2 in
`docs/acceptance/GATE-MATRIX.md` covers `PKG-001`/`PKG-002`/`PKG-003`; its status
is a derived record of admitted evidence, not an input to admission.
