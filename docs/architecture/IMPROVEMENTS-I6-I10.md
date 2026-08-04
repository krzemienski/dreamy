# Improvements I6–I10 — architecture

Status: **proposed**, awaiting operator approval.
Scope: five net-new capabilities beyond R1–R22 and beyond shipped I1–I5.

Every design below was written after reading the live code and probing the
live store during the v1 publish audit. Line references are deliberately
absent — they rot on every edit. Symbols are named instead.

Each improvement closes a defect **this session proved real**, not a
speculative one. The provenance is stated per item, because an improvement
justified by a hypothetical is how scope grows without value.

---

## Sequencing

```
I6  release-gate regression suite ──┐
I9  connector staleness detector ───┤── independent, disjoint modules
I10 doc-example harness ────────────┘

I7  evidence-corpus binding ──> changes acceptance-gate skip semantics
I8  store-write commit guard ─> touches Store lifecycle, used by I7's fixtures
```

* **Wave 1 (parallel):** I6, I9, I10. Disjoint modules, disjoint tables.
* **Wave 2 (sequential):** I8 then I7 — I7's fixtures rely on I8's guard to
  avoid the silent-discard defect described below.

### Migration numbering

Allocated in **ship order, never reserved**. The I2–I5 cycle recorded why:
`Store._migrate` advances with `if current < version`, so a database that
reaches version N permanently skips any lower version added afterwards. The
migration never runs, on every machine that already upgraded, and the defect
surfaces later as a missing table while `schema_version` insists otherwise.

| Improvement | Migration | Notes |
|---|---|---|
| I6 release-gate regression suite | none | test-only |
| I7 evidence-corpus binding | none | reads a marker file, no schema |
| I8 store-write commit guard | none | lifecycle behaviour only |
| I9 connector staleness detector | **next free** | needs a per-source expectation row |
| I10 doc-example harness | none | test-only |

Only I9 needs a migration. It takes the next free version at the moment it
ships, whatever that is.

---

## I6 — release-gate regression suite

### Problem

The v1 publish audit found seven defects in gates that were themselves
reporting green. Each was a *gate* failure, not a product failure:

* the leak sweep passed while a dash-encoded home path shipped, because the
  sweep knew one encoding and the sanitiser knew another;
* `--force-with-lease` was vacuous in a fresh staging repo — no
  remote-tracking ref exists, so the lease had nothing to compare and
  silently degraded to `--force`;
* branch absence was inferred from a failed `git fetch`, which cannot
  distinguish "no branch" from "no network" or "expired token";
* the allowlist parser treated its own 14 comment lines as literal needles;
* the sweep embedded a credential-shaped literal and flagged itself;
* `cat-file -s` failure fell through to `size = 0`, sending an
  unmeasured blob down the scan path;
* build/test gates ran before the tree was re-checked, so anything the test
  run wrote would have published unexamined.

Every one was found by an advisory or a manual read, not by a test. The
release path is the single most destructive operation in the repository —
it force-pushes to a public remote — and it has no regression coverage.

### Design

A test module that exercises `tools/release.sh` and `tools/leak_sweep.py`
against synthetic repositories, asserting the **refusals** rather than the
happy path. A gate that cannot fail is the only kind worth testing.

Cases, each a real defect above:

| Case | Assertion |
|---|---|
| leak in each encoding | sweep exits 1 for literal, dash, underscore, url, base64 |
| clean control | sweep exits 0 |
| oversized blob | reported UNSCANNED, never silently skipped |
| unreadable blob size | reported, not treated as 0 |
| allowlist with comments | comment lines are not needles |
| allowlist absent | release refuses rather than sweeping without one |
| wrong `DREAMY_PUBLISH_REPO` | release refuses before any push |
| unset `DREAMY_PUBLISH_REPO` | release refuses |
| dirty tree | release refuses |
| gate-written artifacts | post-gate check refuses |

Runs against temp repositories with a synthetic `--home`, so it is correct
on any machine and discloses nothing.

### Rejected alternative

Testing the release script by running it against the real remote with a
`--dry-run` flag. Rejected: a dry run that stops before the push cannot
exercise lease semantics or branch-absence handling, which is where three of
the seven defects lived.

---

## I7 — evidence-corpus binding

### Problem

Two acceptance gates compare a machine-local evidence corpus at
`~/.local/share/dreamy/acceptance` against the tracked tree. They skip when
no corpus exists, which is correct on a clean runner — but on a workstation
that holds *another checkout's* corpus they compare two unrelated trees and
fail on the mismatch. Measured during this audit: a fresh clone reported a
tracked-tree count well above its own baseline purely from that mismatch,
with every added anchor pointing into the other checkout's working files.
The failure is noise, and noise in a gate is worse than no gate: it trains
the reader to skip the output.

Three bindings were tried and rejected during the audit:

* `.git` existence — every clone has one;
* first-commit hash — changes on the publish squash, so the published repo
  disowns its own corpus;
* "can the tracked tree resolve the corpus's citations" — circular. It skips
  precisely when a regression creates unresolved anchors, defeating the gate.

### Design

The corpus states its producer; the gate reads it. When a run writes to the
acceptance tree it also writes `.produced-by`, containing a **workspace
instance id**: a UUID generated on first use and stored in the untracked
per-workspace state dir, never committed. A tracked UUID would be identical
in every clone and could not distinguish two checkouts of the same repo —
the same defect that ruled out `.git` existence.

The marker records the workspace instance id alone. Pairing it with the
origin URL was considered and dropped: a fresh clone has no remote until one
is added, and this repository's own publish flow adds a *second* remote, so
the field would be empty or ambiguous exactly when it was needed.

Absence is treated as "not this workspace", so the gate SKIPS rather than
running. Running on an unmarked corpus is what produces the noise this
improvement removes, and a legacy corpus is unmarked by definition. The skip
reason names the marker path, so the fix — one run against this checkout —
is stated rather than discovered.

### Rejected alternative

Deselecting the `acceptance` marker in CI. Rejected during the audit: a clean
runner has no corpus, so those tests already skip harmlessly, and excluding
the marker would forfeit whatever hermetic checks share it to solve a
condition CI never encounters.

---

## I8 — store-write commit guard

### Problem

`Store.close()` closes the connection without committing. A caller that
writes and closes loses every write, silently and with no error. This was hit
during this session while seeding a documentation example: three runs, three
findings, and five observations were written, `close()` was called, and the
resulting report rendered completely empty. Nothing reported a problem — the
data was simply gone.

The production pipeline is unaffected because `run_pipeline` commits
explicitly. That is exactly what makes this dangerous: the defect is invisible
in the path that matters and waits for every other caller.

### Design

`Store.close()` refuses to close a connection with uncommitted changes.
`sqlite3.Connection.in_transaction` reports this exactly. Options considered:

1. **commit on close** — rejected. Turns an abandoned half-write into a
   durable one, which is worse than losing it.
2. **rollback on close** — rejected. Same silence, opposite outcome.
3. **raise on close with pending writes** — chosen. The caller states intent:
   `commit()` or `rollback()` before closing. A test-only
   `close(discard=True)` covers deliberate teardown.

The error states that writes are pending and names the two ways out, because
a guard that fires without saying what to do just moves the confusion. It
does **not** report a count: `sqlite3` exposes `in_transaction` (a boolean)
and `total_changes` (cumulative for the connection, including already
committed rows). Neither is a pending-statement count, and inventing one
from `total_changes` would print a number that is wrong whenever the caller
committed earlier in its life — which is the normal case.

**Raising from `close()` must not mask a real failure.** The overwhelmingly
common call shape is `try: … finally: store.close()`, so a guard that raises
unconditionally would replace the caller's original exception with its own
and hide the actual cause — turning a debugging aid into a debugging
obstacle. The guard therefore raises only when no exception is already
propagating (`sys.exc_info()` is clear). Under an in-flight exception it
rolls back and emits a warning naming the discarded writes: the pending data
was never going to be committed by a failing caller anyway, and the original
traceback survives intact.

---

## I9 — connector staleness detector

### Problem

`dreamy status` reports per-source record counts for the latest run. A source
that has stopped producing reports zero, which is indistinguishable from a
quiet window. Watermarks on a real multi-source install show the difference
matters: some sources track the current day while others last produced
records weeks earlier, and nothing distinguishes "this harness is idle" from
"this harness stopped working".

R11's `ever_ingested` flag already distinguishes "never seen" from "quiet",
but not "was regular, then stopped" — which is the case that means a harness
broke, moved, or changed format.

### Design

Per-source cadence expectation, derived rather than configured: the median
inter-record gap over the last `N = 200` ingested records for that source. A
source whose watermark is older than `k × median_gap`, with `k = 8`, is
**stale**, and stale is surfaced in `status`, in the TUI source panel, and as
a finding.

Both constants are stated here so they can be argued with. `N = 200` is large
enough that one burst of activity cannot halve the median and small enough to
track a harness whose rhythm genuinely changed. `k = 8` means a source is
called stale only after missing roughly eight of its own typical intervals —
chosen because the cost of a false alarm (an ignored detector) is higher than
the cost of noticing a dead connector a day late.

**Minimum sample.** A source with fewer than 20 records has no meaningful
median: two records give a single gap, and one unusual day would define the
baseline forever. Below that floor the source reports `cadence: insufficient
data` and is never called stale. Reporting the reason rather than silently
skipping keeps the gap visible.

Derived, not configured, because a configured threshold is wrong for exactly
the sources that matter — an occasional harness would alarm constantly and be
muted, and a muted alarm is a deleted alarm.

Needs one migration: a `source_cadence` table keyed by `source_id`, holding
the rolling median gap, the sample size it was computed from, and the
timestamp of that computation. Sample size is stored, not just the median, so
a consumer can tell "quiet source with a solid baseline" from "new source
whose median is a guess". Computed at ingest, where the records already are.

### Rejected alternative

A fixed "stale after 7 days" rule. Rejected: on a real install, a harness
used a few times a month would sit permanently stale — a true statement that
nobody would keep reading, which is how a detector becomes decoration.

---

## I10 — doc-example harness

### Problem

Documentation drifts silently. This session found the README claiming 16
subcommands while listing 18, and two live flags (`cost --window`,
`findings --metrics`) documented nowhere — features that existed and worked
but were undiscoverable.

Both were found by scripts written ad hoc during the audit and then thrown
away. The next drift will not be caught.

### Design

A test that extracts every `$ dreamy …` invocation from README, USAGE, and
RUNBOOK, and asserts the CLI accepts it. Not that it exits 0 — several
documented invocations demonstrate error paths and exit 2 by design — but
that argparse recognises the subcommand and every flag. A renamed flag then
fails a test instead of a user.

Paired with a surface completeness check: every subcommand `--help` reports
must appear in the docs, and every documented command must exist.

Mutating, interactive, and placeholder invocations are classified and
**reported**, never silently dropped, so the count of unexercised examples
stays visible.

### Rejected alternative

Doctests. Rejected: output includes real timings and paths that differ per
machine, so a doctest would be either brittle or so loosely matched it proves
nothing.

---

## Acceptance

Each improvement must additionally leave `dreamy doctor` at 0 failures, the
suite green, and `tools/release.sh --dry-run` passing. An improvement that
weakens a gate to pass is refused regardless of what it adds.
