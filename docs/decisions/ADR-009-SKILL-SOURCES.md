# ADR-009 — Skill resolution, pinning, and missing-skill behavior

- **Status:** Accepted
- **Date:** 2026-08-01
- **Requirement IDs:** `BOOT-002`, `SDK-009`, `PKG-002`

## Context

The prompt compiler inlines skill bodies into generated artifacts. Two
questions follow: which skills, and at what content?

Before this decision the answer was implicit. `skill_pins` recorded what a run
*did* use — in the database, after the fact. That supports reproducibility but
not bootstrap: on a pristine machine the database is empty, so nothing states
what must be present *before* the first run. A missing skill degraded to
`missing_on_disk` in a status report and the artifact shipped without it. The
run still exited 0.

A second gap sat next to it. `cli._chain_skill_names` merged overrides via
`getattr(cfg, "prompt_chains", None) or {}`, but `Config` declared no such
field. The expression evaluated to `None` on every call: chains were
documented as configurable and were not.

### What the skills actually look like

Measured on the development machine, over the ten skills the default chains
reference:

| Property | Count |
|---|---|
| Resolvable on disk | 10 / 10 |
| From the user tree (`~/.claude/skills/`) | 8 |
| From a plugin cache | 2 |
| Carrying a machine-readable origin | **1** |

Only `goal-condition-architect` records a repository, via its plugin manifest
(`shannon` 1.2.0, `github.com/krzemienski/shannon`). `ak-research-prompt` sits
in a plugin cache but its manifest declares no repository. The other eight are
hand-placed directories containing a bare `SKILL.md` — no manifest, no version,
no origin.

All three **required** safety skills fall in that last group. This is the
central constraint, and it is a fact about how skills are distributed today,
not a limitation of the design chosen below.

## Decision

### 1. Resolution is by search root, not recorded path

Two roots, in order:

1. `~/.claude/skills/<name>/SKILL.md`
2. `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/<name>/SKILL.md`

Plugin hits are sorted for deterministic selection. An installed path is
machine-local; resolving by root is what lets the same lock verify on a second
machine.

### 2. Identity is content hash

A skill is identified by name and verified by SHA-256 of its body. Location is
provenance. `config/skills.lock` records `observed_absolute_path` — where each
skill resolved on the generating machine, per the specified schema — but
verification never treats it as a destination. A path difference is reported as
`relocated: true` alongside a passing status, because on any machine but the
generating one relocation is expected.

### 3. Required means safety, optional means degradable

| Class | Skills | Missing or drifted |
|---|---|---|
| Required | the three `_constraints` members | verification fails |
| Optional | artifact chain members | reported, run continues |

Declaring a skill optional *is* the statement that the build tolerates its
absence. The safety preamble is not in that category.

### 4. BOOT-002 is satisfied by the branch that applies

The requirement reads: *installable from a lock manifest, **or** fail with an
exact remediation.* Given that 1 of 10 skills has a recoverable source, a
resolver would be a stub that cannot resolve nine entries. So:

- **Installable entry** — remediation names the install:
  `install plugin shannon@1.2.0 from https://github.com/krzemienski/shannon`
- **Hand-placed entry** — remediation says so plainly:
  `manual: no machine-readable origin recorded on disk`

Both then state where to put the file and to re-run `dreamy skills verify`.
`load_lock` rejects a required entry carrying neither `source` nor
`resolution`, so an unactionable required skill cannot enter the lock.

### 5. The product verifies; it never regenerates

`dreamy skills verify` is read-only. Regeneration lives in
`tools/regen-skills-lock.py`, outside the shipped wheel.

A binary that can rewrite its own pins turns a tamper-evident record into a
self-signed one — "verification failed" would be one command away from
"verification passes".

### 6. The lock ships inside the package

`src/dreamy/resources/skills.lock`, resolved through `importlib.resources`.
`config/skills.lock` is a **symlink** to it: one editable source, no drift.

The first implementation defaulted to `config/skills.lock`, a CWD-relative
path. Measured directly: from the checkout it resolved and passed with 10
skills checked; from a temporary directory it raised
`no skill lock at config/skills.lock`. This is the `PKG-002` failure mode —
invisible in development, broken on delivery.

The first attempt to demonstrate this produced a false negative. Shell probes
of the form `cd / && dreamy skills verify` appeared to pass, and were read as
evidence that `Path.resolve()` walked back into the checkout. It does not:
from a real `/`, `config/skills.lock` resolves to `/config/skills.lock` and
does not exist. The probes had never left the repository, because the tooling
running them restores its working directory per invocation. The defect was
real; that evidence for it was not. Foreign-CWD claims are now made only
through `subprocess` with an explicit `cwd=`, which is what
`tests/integration/test_wheel_lock_shipping.py` uses.

### 7. Chains are configurable, and now actually are

`Config.prompt_chains` is a declared field. A named chain **replaces** that
default outright; unnamed chains keep theirs. Malformed shapes are rejected at
load, because a bad chain otherwise yields zero pinned skills silently.

## Consequences

**Verification runs on a machine that has never run dreamy.** It reads the lock
and the filesystem, no database. That is the bootstrap case.

**Nine of ten skills need manual installation.** The lock states this rather
than implying otherwise. Improving it requires the skills to be distributed
with manifests — a change upstream of this repository.

**A wheel-only exclude would defeat source-level tests.** Unit tests resolve
through `importlib` while `src/` is importable under pytest, so they assert on
the source tree. Adding `exclude = ["src/dreamy/resources/skills.lock"]` to the
wheel target produces a lock-less wheel that passes 5/5 of them.
`tests/integration/test_wheel_lock_shipping.py` builds a real wheel, installs
it into a clean venv, and runs the console script from a foreign CWD. Verified
against that injected exclude: the integration test fails 3, the unit tests
still pass 5.

**Skill improvements do not reach artifacts automatically.** A pinned body is
frozen until `dreamy skills refresh` re-pins it. That is the cost of
byte-identical re-emit, and it is why refresh exists as an explicit action.

## Alternatives rejected

**Record install paths for reuse.** Directly contradicts portability: a path
under one developer's home cannot be honoured elsewhere. The path is retained
as provenance only.

**Fail the run on any missing skill.** Would make an unavailable optional
enrichment skill block a deterministic artifact that does not need it.

**Vendor skill bodies into the repository.** Removes the resolution problem and
replaces it with a synchronisation problem, while silently forking every
upstream skill.

**Let `dreamy` regenerate its own lock.** See §5.
