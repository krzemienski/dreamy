## What changed

<!-- Describe the change. Name the requirement ID(s) from
     docs/specifications/SPEC-DREAMY-V2.md if applicable. -->

## Functional validation

**Required:** the real command(s) you ran and the real output you
observed. Do not describe expected behavior — paste actual output. New
functionality is validated through real interfaces per the standing
no-unit-test directive in `CONTRIBUTING.md`, not through new test files.

```
# command
dreamy <subcommand> ...

# real output
...
```

- [ ] I ran the change against real data (or acceptance-frozen real data),
      not a mock/fixture, and the output above is that real output.
- [ ] If this touches redaction, bundle export, or a doctor check, I ran
      `dreamy doctor` and the relevant check(s) report `PASS`:
      <!-- e.g. N4-1, R14-1, R9-1 -->

## Opt-in acceptance suites

- [ ] Not applicable — this change doesn't touch `acceptance.py`,
      `correlate.py`, `store.py`, or a connector.
- [ ] Applicable, and I ran it: <!-- paste the DREAMY_RUN_ACCEPTANCE=1 or
      DREAMY_CURRENT_RUN_MANIFEST result, or state why it wasn't run -->

## Gates

```bash
ruff check src tests
mypy src/dreamy
pytest -q
```

- [ ] `ruff check src tests` — exit 0
- [ ] `mypy src/dreamy` — exit 0
- [ ] `pytest -q` — exit 0 (paste the summary line, e.g. `371 passed, 12 skipped`)

## Docs updated

- [ ] README.md
- [ ] `docs/operations/USAGE.md`
- [ ] `SECURITY.md`
- [ ] Architecture docs (`docs/architecture/ARCHITECTURE.md`)
- [ ] N/A — no user-facing or contributor-facing behavior changed

## Ownership boundaries respected

- [ ] No new import from one connector into another
- [ ] `claude_agent_sdk` still imported only from `agent_sdk.py`
- [ ] Anything written to `state.db`, a compiled artifact, a log line, or a
      bundle passes through `redact.py` first
