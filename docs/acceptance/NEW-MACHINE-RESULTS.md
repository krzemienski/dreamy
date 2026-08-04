# NEW-MACHINE-RESULTS

Requirements: `BOOT-001`, `BOOT-002`

## Status: NOT RUN

**No pristine-machine execution has occurred.** `BOOT-001` is unsatisfied and
`GATE-MATRIX.md` G1 stays BLOCKED.

This file exists so the absence is recorded rather than inferred from a missing
document. It will be replaced by real results when a clean machine is available.

## What a pristine run requires

A host that has never built or installed Dreamy: no `~/.local/share/dreamy/`, no
prior venvs, no ambient `claude-agent-sdk`. The last condition is not
theoretical — the development host carries an unrelated `claude-agent-sdk 0.1.68`
in its ambient interpreter, and reading its numbers as authoritative previously
produced a false "the lock file is stale" conclusion
(`SDK-VERSION-LOCK.md`, "Which environment to verify in").

The run must execute `BUILD-FROM-ZERO.md` end to end and record, per step: the
exact command, its exit code, and the SHA-256 of every artifact produced.

## What was executed instead, and where

`BUILD-FROM-ZERO.md` was executed on the **development host** on 2026-08-01,
against clean trees in `/tmp`. This validates the procedure. It does not
validate machine independence, because the host is not pristine.

| Step | Command | Exit | Notes |
|---|---|---|---|
| Clean tree | `git archive --format=tar HEAD \| tar -x -C /tmp/bfz` | 0 | committed state only |
| Build deps | `PIP_USER=0 ./v/bin/pip install --require-hashes -r config/requirements-build.lock` | 0 | 5 hash-pinned packages |
| Build wheel | `./v/bin/python -m hatchling build -t wheel` | 0 | `python -m build` unavailable |
| Install | `PIP_USER=0 pip install dreamy-2.0.0.dev0-py3-none-any.whl` | 0 | separate venv |
| `dreamy --help` | from `mktemp -d` | 0 | |
| `dreamy status` | from `mktemp -d` | 0 | JSON |
| `dreamy skills verify` | from `mktemp -d` | 0 | JSON |

Working directory was measured by passing `cwd=` to `subprocess.run` and
printing `pathlib.Path.cwd()` from inside the child — not by a shell `cd`, for
the reason recorded in `INCIDENT-260801-cwd-contamination.md`.

### Artifact hashes

| SHA-256 | Artifact | Size | Source tree |
|---|---|---|---|
| `3689e2384cb438ac1f4c00ff035656317a56bca302fbc20e41b17769dc1abcf4` | `dreamy-2.0.0.dev0-py3-none-any.whl` | 162401 | `git archive HEAD` |
| `e10901ade382575e22579946e31e9f276232e6d4b26803bfa51aae23493ff8e5` | `dreamy-2.0.0.dev0-py3-none-any.whl` | 168811 | working tree |

The two differ because the working tree carries uncommitted changes — most
visibly `skills verify`, which the `HEAD` wheel rejects with exit 2. Recorded
because the difference is the reproducibility signal: identical version,
different bytes, different behaviour.

Reproducibility across machines is **not** established by these hashes. A
pristine run must confirm whether a wheel built from the same ref hashes
identically elsewhere.

## Defects found by executing the procedure

1. `SDK-VERSION-LOCK.md:15` placed `PIP_USER=0` before `venv` instead of on the
   `pip` call, which fails inside a virtualenv. **Fixed**, and the corrected
   sequence re-run: it reproduces the locked 145 exports / 45 option fields.
2. The build frontend `build` is not in `requirements-build.lock`; only the
   backend `hatchling` is. **Documented** in `BUILD-FROM-ZERO.md` step 3.

## Blocking `BOOT-001`

- No pristine machine.
- `dreamy doctor` does not exist. Registered subcommands: `run`, `install`,
  `uninstall`, `status`, `skills`, `tui`, `verify-run`.
- `textual` is not installed, so `dreamy tui` cannot be exercised here.
