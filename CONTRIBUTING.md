# CONTRIBUTING

Thanks for looking at dreamy. This project takes correctness over speed
seriously — every rule below exists because a real defect got past a
weaker version of it. Read it before your first PR.

## Setting up for development

Install into a **durable venv outside the checkout**, not editable in-place:

```bash
python3 -m venv ~/.venvs/dreamy
~/.venvs/dreamy/bin/pip install -e ".[dev]"
```

`dreamy install` (the launchd scheduler) refuses to run against an
interpreter that lives inside the checkout or a temp directory — a checkout
can move or get deleted, and a scheduled job pinned to a path that
disappears fails silently. This isn't optional for scheduling, so set it up
this way from the start rather than retrofitting it later. If you only need
to run the test suite and lint/type checks (no scheduling), an editable
install from the checkout's own `.venv/` works fine for that subset of
workflows — just don't rely on it for `dreamy install`.

Add the `agent` extra if you're touching anything under `agent_sdk.py`, and
`tui` if you're touching `dreamy.tui.*`:

```bash
~/.venvs/dreamy/bin/pip install -e ".[dev,agent,tui]"
```

## Coding conventions

Enforced by the tools below, not by style preference — a change that
violates the config these tools read is not a matter of opinion.

### Ruff (`pyproject.toml` → `[tool.ruff]`)

```toml
line-length = 120
src = ["src", "tests"]
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- `E` — pycodestyle errors.
- `F` — Pyflakes (unused imports, undefined names, etc.).
- `I` — import sorting (`isort`-equivalent).
- `UP` — `pyupgrade`: flags code that could use a newer Python 3.11+
  idiom.
- `B` — `flake8-bugbear`: catches likely-bug patterns (mutable default
  arguments, unused loop variables that shadow, etc.).
- Line length 120, not the Black default of 88 or Ruff's default of 88 —
  this project's line length is set explicitly and deliberately wider.
- Target version `py311` — do not write code that only works on 3.12+;
  `requires-python = ">=3.11"` is the floor the linter checks against.

Run it exactly as CI/the PR gate does:

```bash
ruff check src tests
```

### Mypy (`pyproject.toml` → `[tool.mypy]`)

```toml
[tool.mypy]
python_version = "3.11"
files = ["src/dreamy"]
ignore_missing_imports = true
```

- Checks `src/dreamy` only — **not** `tests/`. Test code is not
  type-checked by this gate; do not assume a green `mypy` run says
  anything about test-file type correctness.
- `ignore_missing_imports = true` — third-party packages without stubs
  do not fail the check on their own account. This does not relax
  checking of *your* code; it only silences complaints about missing
  stub files for dependencies.

Run it exactly as the PR gate does:

```bash
mypy src/dreamy
```

## Ownership

The module layers in `docs/architecture/ARCHITECTURE.md` define where new
code belongs. Two structural rules that matter for reviewers, not just
architecture readers:

- **Connectors never import each other.** Each connector
  (`connectors/{claude,codex,omp_pi,opencode,router}`) reads exactly one
  harness's store. A PR that adds an import from one connector into
  another is a boundary violation regardless of how convenient it looks.
- **The SDK is confined to one import site.** `agent_sdk.py` is the only
  module under `src/dreamy/` that imports `claude_agent_sdk`. Any other
  module that starts importing it directly is bypassing the isolation
  that keeps "the `agent` extra is absent" a contained failure instead
  of an import error at startup.
- **Secrets never bypass `redact.py`.** Anything written to `state.db`,
  a compiled prompt artifact, a log line, or a bundle must have passed
  through `redact()` / `redact_dict()` / `redact_value()` /
  `redact_text()` first. See `SECURITY.md` for why this is exact-value
  matching against known credential sources, not a general regex.

If a change needs to cross one of these boundaries, that is a design
question for the PR description to address explicitly, not something to
route around silently.

## The standing no-unit-test directive

**Do not add new unit test files for new work in this project.** This is a
deliberate, standing rule, not an oversight:

- Validation for new behavior happens through **real interfaces** —
  running the actual CLI subcommand against real (or acceptance-frozen
  real) data, reading the actual output, and citing the actual bytes
  produced. See `docs/acceptance/GATE-MATRIX.md` for the shape this takes
  at the project level.
- The existing `tests/` suite is maintained as-is (fix a regression there
  if your change breaks it), but new coverage for new functionality should
  be **functional validation**, not a new `test_*.py` mock/fixture file.
  A test that mocks a connector or fakes a store proves the mock behaves
  as written, not that dreamy behaves correctly against a real session
  store — and this project has a documented history (see
  `docs/operations/RUNBOOK.md`) of real defects that a mock-based test
  would not have caught.
- If your PR description can't point to a real command, real arguments,
  and real captured output that exercises the change, the change is not
  validated yet, no matter what `pytest -q` reports.

This directive covers **new** work. It does not ask you to delete or avoid
touching the existing test suite — see "Running the existing suite" below
for how that suite is actually gated and what it does and doesn't prove.

## Functional validation expectations

For any change to `src/dreamy/`, your PR description should show:

- The exact command you ran (`~/.venvs/dreamy/bin/dreamy <subcommand> ...`
  or `.venv/bin/python -m dreamy <subcommand> ...`).
- The real output it produced — not a hand-written example of what it
  *should* produce.
- For anything touching redaction, bundle export, or doctor checks:
  the relevant `dreamy doctor` check IDs (e.g. `N4-1`, `R14-1`, `R9-1`)
  and whether they report `PASS` after your change.
- For anything touching a connector, `correlate.py`, or `store.py`: state
  explicitly whether the relevant opt-in acceptance suite (see below) was
  run, and cite the result — the default suite's silence on those paths is
  not evidence they still work.

## Running the existing suite

### The actual gate

```bash
ruff check src tests    # exit 0
mypy src/dreamy         # exit 0
pytest -q               # exit 0
```

Current state on this checkout (measured 2026-08-03, in the checkout's own
`.venv`): **371 passed, 12 skipped**, ruff exit `0`, mypy exit `0`. All
three commands are required for a PR to be mergeable; none of them
individually is sufficient (see the next section for why a green
`pytest -q` alone proves less than it looks like it proves).

Run with the durable venv or the checkout's own `.venv` — either works for
this subset:

```bash
.venv/bin/python -m pytest -q
```

### The skips are acceptance-gated, not failures

`pytest -q` reporting a non-zero skip count is the correct, expected
outcome on a normal developer machine — **do not** treat a skip count > 0
as something to chase down or eliminate by force-running. The skips fall
into two families, both deliberate:

1. **`@pytest.mark.acceptance` tests**, gated behind
   `DREAMY_RUN_ACCEPTANCE=1`. These hit real disk I/O against real
   connector source data (`~/.claude/projects`, `~/.codex`, `~/.omp`,
   etc.), some of which hold gigabytes of session history. They are
   opt-in because they are slow and side-effecting, not because they are
   unimportant — see `tests/conftest.py`'s `frozen_source_overrides`
   fixture, which snapshots every live connector source into a temp
   directory before these tests run against the frozen copy, specifically
   so a concurrent live write cannot fake or break an idempotency result.

2. **`test_current_run_citations_resolve`**
   (`tests/integration/test_citation_selfcheck.py`), gated behind
   `DREAMY_CURRENT_RUN_MANIFEST`. It verifies citations in a validated run
   manifest actually resolve; without a manifest path there is nothing to
   validate, so it skips rather than fabricating a pass.

**Practical consequence for reviewers:** `pytest -q` exiting `0` with a
non-zero skip count is necessary evidence for a merge but is **not, by
itself, evidence that the acceptance-gated paths have been exercised this
phase.** Check `docs/acceptance/GATE-MATRIX.md` for what is actually
claimed `PASS` versus `BLOCKED` — a green default suite and an admitted
gate are two different facts, and only the gate matrix records the second
one.

### Running the opt-in suites

```bash
DREAMY_RUN_ACCEPTANCE=1 pytest -m acceptance
DREAMY_CURRENT_RUN_MANIFEST=/path/to/validated/manifest.json pytest tests/integration/test_citation_selfcheck.py
```

Neither is part of the default `pytest -q` invocation and neither is
required for a PR to merge — but a PR that changes `acceptance.py`,
`correlate.py`, `store.py`, or anything a connector touches should state
in its description whether the relevant opt-in suite was run, and cite
the result, because the default suite's silence on those paths is not
evidence they still work.

## Commit policy

- Commit messages should name the requirement ID(s)
  (`docs/specifications/SPEC-DREAMY-V2.md`, format `PREFIX-NNN`) a change
  addresses where one exists, so `git log --grep` against a requirement
  ID is a usable audit trail.
- A commit that fixes a bug found during development should explain *why*
  the bug was possible (root cause), not just what changed. Several
  modules under `src/dreamy/` carry inline comments doing exactly this at
  the fix site (e.g. `redact.py`'s explanation of why hex-token matching
  had to move from pattern-based to exact-value); match that standard in
  the commit message too.
- Prefer separating "implementation" and "regression test added for this
  bug" into distinct commits when the fix touches the existing `tests/`
  suite — a reviewer benefits from seeing the test fail against the
  pre-fix code. This is orthogonal to the no-new-unit-test directive
  above, which is about *new functionality*, not regression coverage for
  an existing bug in the maintained suite.

## Opening a pull request

Mechanically, before requesting review:

```bash
ruff check src tests
mypy src/dreamy
pytest -q
```

All must exit `0`. PR descriptions should identify:

- Which requirement ID(s) the change addresses (if applicable).
- The real command(s) you ran and the real output you observed — see
  "Functional validation expectations" above. This is the primary
  evidence a reviewer will look for; it is not optional supplementary
  material.
- Whether any opt-in acceptance suite applies to your change, and if so,
  whether it was run and what it reported.
- Which docs were updated as a result (README, `docs/operations/USAGE.md`,
  `SECURITY.md`, architecture docs) — a behavior change without a doc
  update is treated as incomplete, not deferred.

A PR that only shows `pytest -q` passing, with no real-command evidence for
the actual change, will be asked for the missing evidence before review
proceeds.
