# BUILD-FROM-ZERO

Requirements: `BOOT-001`, `BOOT-002`, `PKG-001`, `PKG-002`

Build and install Dreamy on a machine that has never seen it. Every command
below was executed while writing this document; the deviations that were found
are recorded in **Known deviations** rather than silently corrected in prose.

This is not a pristine-machine acceptance run. It was executed on the
development machine in throwaway directories (`/tmp/bfz`, `/tmp/bfz2`) using
`git archive` to obtain a clean tree. `BOOT-001` requires a genuinely pristine
machine; see `docs/acceptance/NEW-MACHINE-RESULTS.md`.

## Prerequisites

| Component | Version used | Required |
|---|---|---|
| Python | `3.11.6` | yes — `requires-python` in `pyproject.toml` |
| Git | `2.55.0` | yes — `cli.py` exits 127 without it |
| SQLite | `3.42.0` (stdlib `sqlite3`) | yes |
| Claude CLI | `2.1.220` | only for agent features |
| Platform | `Darwin 27.0.0 arm64` | see `SDK-VERSION-LOCK.md` |

Nothing else. No `uv`, no `pipx`, no network access beyond PyPI.

## 1. Obtain a clean tree

```sh
mkdir -p /tmp/dreamy-build && cd /tmp/dreamy-build
git -C <checkout> archive --format=tar HEAD | tar -x -C /tmp/dreamy-build
```

`git archive HEAD` gives exactly what is committed. Uncommitted work is
excluded — deliberately, because a build must be reproducible from a ref.

> While writing this, that property was demonstrated the hard way: a wheel built
> from `HEAD` rejected `dreamy skills verify` with exit 2, which looked like a
> documentation error. It was not. `verify` was an uncommitted working-tree
> change. The build was correct and the tree was ahead of it.

## 2. Create the build environment

```sh
python3 -m venv v
PIP_USER=0 ./v/bin/pip install --require-hashes -r config/requirements-build.lock
```

`PIP_USER=0` must be set **on the `pip` invocation**, not before `venv`.

> Setting it earlier fails:
> `ERROR: Can not perform a '--user' install. User site-packages are not visible
> in this virtualenv.`
> `SDK-VERSION-LOCK.md` line 15 shows the broken ordering.

`--require-hashes` is not optional. The lock pins `hatchling==1.31.0` and its
transitive closure by SHA-256; without the flag, a compromised index can
substitute a build backend, which executes during the build.

## 3. Build the wheel

```sh
./v/bin/python -m hatchling build -t wheel
```

Produces `dist/dreamy-2.0.0.dev0-py3-none-any.whl`.

`python -m build` does **not** work: `config/requirements-build.lock` pins the
backend (`hatchling`) and not the `build` frontend. Invoking hatchling directly
keeps the dependency surface at five hash-pinned packages.

## 4. Install into a separate environment

```sh
python3 -m venv /tmp/dreamy-run
PIP_USER=0 /tmp/dreamy-run/bin/pip install /tmp/dreamy-build/dist/dreamy-*.whl
```

Separate from the build venv on purpose: it proves the wheel carries its own
runtime dependencies rather than inheriting the builder's.

## 5. Verify from outside the checkout

The point of `PKG-002` is that the installed product does not depend on the
source tree. Verify it from a directory that is not the checkout:

```sh
cd "$(mktemp -d)"
/tmp/dreamy-run/bin/dreamy --help
/tmp/dreamy-run/bin/dreamy status
/tmp/dreamy-run/bin/dreamy skills verify
```

Observed: all three exit 0. `status` and `skills verify` emit JSON.

### Measuring the working directory honestly

A shell probe of the form `cd / && dreamy status` **does not prove anything**
when run through tooling that restores its own working directory per
invocation. That mistake produced four false acceptance artifacts on
2026-08-01; see `INCIDENT-260801-cwd-contamination.md`.

Use one of these instead:

```python
subprocess.run([...], cwd=far)          # explicit, preferred
```
```python
os.chdir(far)                            # in-process, verify with Path.cwd()
```

and print `pathlib.Path.cwd()` from inside the command before trusting output.
`tests/unit/test_foreign_cwd_claims.py` pins this.

## 6. Editable install (development only)

```sh
PIP_USER=0 pip install -e <checkout>
```

Never use `PYTHONPATH` or repository-relative imports. `PKG-002` requires the
installed product to resolve its own resources through `importlib.resources`;
`src/dreamy/resources/skills.lock` ships inside the wheel and
`config/skills.lock` is a symlink to it, so the two cannot drift.

## Known deviations found while executing this document

| # | Where | Defect |
|---|---|---|
| 1 | `SDK-VERSION-LOCK.md:15` | `PIP_USER=0 python3 -m venv …` then plain `pip install` fails inside the venv. `PIP_USER=0` belongs on the `pip` call. |
| 2 | this document, draft | `python -m build` was assumed available. It is not in `requirements-build.lock`; use `python -m hatchling build -t wheel`. |

Both were found by running the commands, not by review.

## What this does not establish

- **`BOOT-001`** — a pristine machine. This ran on the development host.
- **`dreamy doctor`** — the subcommand does not exist. Registered subcommands
  are `run`, `install`, `uninstall`, `status`, `skills`, `tui`, `verify-run`.
  `GATE-MATRIX.md` G1 already records this.
- **TUI** — `textual` is not installed here, so `dreamy tui` is unexercised.
