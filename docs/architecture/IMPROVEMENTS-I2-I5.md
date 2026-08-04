# Improvements I2–I5 — architecture

Status: **approved for implementation** (operator, this session).
Scope: four net-new capabilities beyond the R1–R22 specification.

Every design below was written after reading the live code and probing the
live store. Line references are deliberately absent — they rot on every edit.
Symbols are named instead.

---

## Sequencing

The four improvements are **not** independent. The dependency graph decides
what may run in parallel:

```
I2 source-format canary ──┐
                          ├── independent, no shared surface
I5 bundle reverse-import ─┘

I3 cumulative spend ledger ──> new table + new CLI surface
I4 finding lifecycle metrics ─> reads the SAME observation ledger I3 writes near
```

* **Wave 1 (parallel):** I2 and I5. Disjoint modules, disjoint tables.
* **Wave 2 (parallel):** I3 and I4.

### Migration numbering — corrected during implementation

The first draft of this section RESERVED v8 for I3 and v9 for I4, and that was
a latent correctness bug, not a bookkeeping preference.

`Store._migrate` advances with `if current < version`. A database that reaches
version N therefore **permanently skips** any version lower than N added
afterwards — the migration never runs, on every machine that had already
upgraded, and the defect surfaces much later as a missing table while
`schema_version` insists the migration was applied.

Since I5 shipped first, it took **v8**. Versions are allocated in SHIP ORDER,
never reserved:

| Improvement | Migration | Status |
|---|---|---|
| I5 archival import | **v8** | shipped |
| I3 cumulative spend ledger | next free (v9) | pending |
| I4 finding lifecycle metrics | next free after I3, if any | pending — may need none |

I4 is expected to need no migration at all: its metrics derive from the
existing observation ledger, exactly as `_delta_states` does.

Rationale for not running all four at once: I3 and I4 both touch
`store._LADDER` and both add a CLI subcommand. Concurrent edits to the same
ladder dict would conflict; concurrent edits to two disjoint modules will not.

---

## I2 — source-format canary

### Problem

Every connector parses a third-party on-disk format it does not control.
When a harness changes its schema, the connector does not crash — it silently
yields fewer records, or none. The existing zero-count warning cannot tell
"this source is quiet" from "this source's format moved out from under us".

This is the same failure class as D1 (the `claude: no records in 30d` false
negative, fixed this session): the system reported a state it had not
observed. D1 fixed the *reporting*; I2 addresses the *detection*.

### Design

A canary is a **structural assertion about a real file**, evaluated at scan
time, not a schema copy.

For each connector, record the shape it depends on:

* `claude` — `*.jsonl`, one JSON object per line, keys `timestamp`, `type`
* `codex` / `opencode` / `omp` / `pi` — the equivalent per-connector shape
* `nine_router` — its request-log shape

At discovery, the connector samples the **newest** file it can see and asserts
the shape. Three outcomes:

| Outcome | Meaning | Behaviour |
|---|---|---|
| `ok` | shape matched | scan proceeds |
| `drifted` | file present, shape unrecognised | scan proceeds, **loud warning** naming the file and the missing key |
| `absent` | no files to sample | existing "unavailable" path, unchanged |

`drifted` must NOT abort the run. A partial ingest from five healthy sources
is worth more than a hard failure, and R6f's bounded-warning precedent already
establishes that a degraded source is a warning, never a crash.

### Surface

* `IngestStats.format_health: dict[str, str]` — source_id -> outcome
* surfaced in `dreamy status --json` and the web `/api/sources` payload
* `dreamy doctor` gains check `I2-1`: every available source reports `ok`

### Why not schema-version pinning

Rejected: none of the six harnesses publish a schema version. Pinning a hash
of a sample file would fire on every benign content change. The assertion must
be about *structure the connector actually reads*, which is the narrowest
thing that can break it.

---

## I3 — cumulative spend ledger

### Problem

`config.spend_cap_usd` is enforced **per run**. Verified in `config.py`: the
cap is validated as positive-or-null, and agent analysis is disabled until a
positive cap is explicit. There is no cumulative accounting, so at the
scheduled 4 runs/day a `$5.00` per-run cap permits `$20/day` and `~$600/month`
with nothing in the system able to observe that total, let alone stop it.

The specification's own Q5 arithmetic identifies this; it was never closed.

### Design

New table, migration **v8**:

```sql
CREATE TABLE spend_ledger (
    run_id      TEXT PRIMARY KEY,
    started_ms  INTEGER NOT NULL,
    cost_usd    REAL NOT NULL DEFAULT 0,
    model       TEXT,
    call_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_spend_started ON spend_ledger(started_ms);
```

One row per run. `runs` already records agent cost per run, but it is not
indexed for time-window rollups and mixes concerns; a dedicated ledger keeps
the cap query cheap and independent of run retention policy.

Two new config keys:

* `spend_cap_daily_usd: float | None` — rolling 24h ceiling
* `spend_cap_monthly_usd: float | None` — rolling 30d ceiling

Both default `None` (disabled), matching the existing conservative posture of
`spend_cap_usd`. A null cap must NOT mean "unlimited by accident" — it means
the operator has not opted in, exactly as the per-run cap already behaves.

### Enforcement point

Before the agent layer starts, sum the ledger over the trailing window. If
either cumulative cap would be exceeded, **skip the agent layer** and record
the reason. This mirrors the existing `agent_run_status: "disabled"` path
already visible in real run output, so the downstream surfaces need no new
state to render.

Enforcement is **pre-flight, not mid-flight**: a cap checked after spending is
not a cap. A run that would breach the ceiling does not start the agent layer
at all.

### Surface

* `dreamy cost --window daily|monthly` — the rollup
* `dreamy status --json` gains `spend: {daily_usd, monthly_usd, caps, headroom}`
* `dreamy doctor` check `I3-1`: ledger row count matches runs with agent spend

---

## I4 — finding lifecycle metrics

### Problem

`ReadStore._delta_states` already derives the five R13 states
(`new | persisting | resolved | regressed | dismissed`) from the observation
ledger. That answers "what is the state of this finding **now**". It cannot
answer the questions an operator actually asks over time:

* how long does a finding of severity X survive before it is resolved?
* which categories regress after being fixed?
* is the backlog growing or shrinking?

The data to answer all three is already in the ledger. Nothing aggregates it.

### Design

**No new table.** This is a pure read-side aggregation over existing
observation rows, computed in `read.py` alongside `_delta_states`.

Metrics, per category and per severity:

| Metric | Definition |
|---|---|
| `time_to_resolve_ms` | median ms from first observation to the run that resolved it |
| `regression_rate` | resolved-then-regressed / total resolved |
| `backlog_delta` | (new + regressed) − resolved, over the window |
| `age_p50` / `age_p90` | age distribution of currently-open findings |

`not_observed` semantics carry over from R21: a finding with insufficient
history is **excluded from the denominator**, never counted as a success or a
failure. The efficacy report already establishes this convention and the
operator already reads it that way.

Migration **v9** is reserved for I4 only if a materialised rollup proves
necessary for performance. First implementation computes live; the reservation
exists so I3 and I4 can be authored concurrently without a ladder collision.

### Surface

* `dreamy findings --metrics`
* TUI Findings view gains a summary header
* web `/api/findings/metrics`

### Why derived, not stored

Same reasoning that made `_delta_states` derived: a stored lifecycle column
would need to be recomputed on every run anyway, and a stored value that
disagrees with the ledger is worse than no value. The ledger is the authority.

---

## I5 — portable bundle reverse-import

### Problem

`dreamy bundle` exports a self-contained HTML file per project. It is a
one-way door: the receiving machine can read it in a browser and nothing else.
Two real workflows are blocked — moving a project's history between machines,
and archiving a project before deleting its checkout.

The bundle already contains the data. It is simply not machine-readable.

### Design

Embed a **structured payload** in the exported HTML, and add a reader.

```html
<script type="application/json" id="dreamy-bundle">{...}</script>
```

`<script type="application/json">` is inert — browsers do not execute it, so
the bundle stays a safe artifact to hand to a person while becoming
machine-readable.

### Payload contents — corrected after auditing the real store

The first draft of this section said "turns (fingerprints only, never raw
content)". Auditing the live store proved that promise unachievable with the
columns as named, and the correction is load-bearing:

| Column | What it actually holds | Decision |
|---|---|---|
| `turns.content_fingerprint` | **NOT a hash.** Truncated user prose — measured across 20,000 rows: length 2–200, median 197. A recursive value audit found 2,216 URLs and 108 absolute paths inside it. | re-hash; never export |
| `sessions.native_id` | absolute `$HOME` path for `claude` and `omp` (4,146/4,267 rows in the audited project) | hash with source_id |
| `sessions.raw_path` | `$HOME` in 11,129/11,129 rows | drop |
| `turns.error_text` | free-form error prose; 0 populated rows today | drop **by rule**, not by measurement |
| `turns.file_paths_json` | 444,415 rows, 0 with `$HOME` | drop anyway — repo-relative paths still disclose directory structure and unreleased names |
| `sessions.id`, `turns.id` | already opaque 16-hex `stable_id` digests, 0 rows containing `$HOME` | safe to export as-is |

Contract: **identity, timestamps, and digests. No prose, no paths, no URLs.**
Verified by a recursive allowlist audit over every string in the payload, not
by a substring scan — the substring scan is what missed `native_id` twice.

### Import target: an ARCHIVAL namespace, not the live tables

A receiver holding only hashes **cannot** recompute canonical ids.
`turns.id = stable_id("turn", session_id, ts, role, tool_name, fingerprint)`
consumes the RAW fingerprint, which is exactly the value the export refuses to
carry. Claiming an import "preserves dedupe semantics" would therefore be
false — the receiver can never derive the same id the writer did.

Import therefore writes to dedicated archival tables
(`imported_sessions`, `imported_turns`) keyed by
`(bundle_id, exported_session_id)`:

* live ingestion and archival import never share a key space, so an import
  structurally cannot collide with, overwrite, or resurrect a local row;
* re-importing the same bundle is idempotent on the bundle's own ids;
* nothing pretends a hashed record is the same object the writer had.

New command `dreamy import <bundle.html>`:

1. parse the embedded JSON; reject on missing/unknown `schema_version`
2. write into the archival tables under the bundle's own id
3. mark every row `provenance='imported'`
4. report a summary of what was written

### Identity handling — corrected during implementation

An earlier draft proposed exporting HMACs of `native_id` and
`content_fingerprint` under a **per-bundle salt carried in the payload**. That
was abandoned for two independent reasons, both established by measurement
rather than argument:

1. **A key that ships with the ciphertext is not a secret.** A receiver needs
   the salt to use the digest, so it must travel in the payload — at which
   point anyone holding the bundle can hash their own guesses against it. The
   inputs are low-entropy (`~/.claude/projects/<guessable-name>`), so a public
   salt defeats precomputed rainbow tables and nothing else. Demonstrated:
   given only an exported `stable_id`, recomputing it from a guessed path
   reproduced the id exactly, while three wrong guesses did not.
2. **The digests were redundant.** Measured on the live store, `sessions.id`
   and `turns.id` are already unique 1:1 with their rows (11,129/11,129 and
   444,415/444,415 distinct). Import keys on `(bundle_id, export_id)`, so a
   per-row content key buys the importer nothing.

**What ships instead:** every canonical id is replaced by an HMAC computed
under a random key generated per bundle and **discarded immediately after
export** — never serialised. Because the key never travels:

* a guess cannot be confirmed: there is nothing to hash against;
* two bundles of the same project share no ids and cannot be correlated;
* re-importing the same file stays idempotent, since the ids are in the file.

The cost, stated rather than hidden: a receiver cannot merge two exports of
one project, because nothing links them. Each bundle is a self-contained
snapshot, which is what both I5 use cases need.

Tamper protection is separate from identity: `imported_bundles.payload_sha256`
stores a canonical SHA-256 over the validated payload, and a re-import whose
digest differs is REFUSED rather than reported as "already imported".

### Non-negotiables

* **Redaction survives the round trip.** The export path already portabilizes
  home paths and runs a fail-closed canary guard on the rendered bytes. The
  JSON payload is assembled *before* that guard and must be covered by it —
  otherwise the guard's own mutation test (which currently kills a neutered
  portabilizer) would pass while the payload leaked. The guard runs on final
  bytes, so this holds by construction; a dedicated import-side canary check
  is added regardless, because a guarantee that depends on ordering should be
  asserted, not assumed.
* **Import is additive, never destructive.** No import path may update or
  delete a locally-observed row. Namespacing enforces this structurally rather
  than by convention.
* **Turn content is never embedded** — fingerprints only, matching what the
  store already persists.

### Surface

* `dreamy import <path>` — additive, reports a written-row summary
* `dreamy doctor` check `I5-1`: export -> import -> export round trip is stable

### Why not a separate export format

Rejected: a second artifact (`.dreamy-bundle.json`) doubles the export surface
and immediately drifts from the HTML. One artifact that is both human-readable
and machine-readable cannot disagree with itself.

---

## Validation contract (all four)

Per the standing directive, **no unit tests**. Each improvement is validated
through the real interface, against the real store, with captured output:

| Improvement | Proof |
|---|---|
| I2 | corrupt a real sample file in a temp copy, run against it via `--source-path`, show `drifted` reported and the run still completing |
| I3 | seed the ledger from real run history, show a cumulative cap blocking the agent layer pre-flight |
| I4 | `dreamy findings --metrics` over the live store (81 real findings), numbers hand-checked against the ledger |
| I5 | export a real project, import into a scratch store, diff the two, plus the mutation test that the canary guard still fires |

Each must additionally leave `dreamy doctor` at 0 failures and the suite green.
