# RELEASE

How a change moves from a branch to a released, verifiable artifact.

**Status honesty up front:** this document describes the intended process.
The push and pull-request steps are **currently unexecutable** —
`git remote -v` on this checkout returns nothing:

```
$ git remote -v
$
```

No remote is configured. There is nowhere to push a branch, nowhere to
open a pull request, and nowhere to publish a tag or a GitHub release.
This blocks requirement `GIT-001` (*"Every phase MUST be delivered
through a pushed branch and reviewed pull request"*) and is recorded as
gate `G13` — **BLOCKED**, no partial credit — in
`docs/acceptance/GATE-MATRIX.md`:

| Gate | Req IDs | Requirement | Status | Evidence |
|---|---|---|---|---|
| G13 | GIT-001, GIT-002, GIT-003, REL-001 | Protected-branch merge, signed tag, GitHub release, checksums | **BLOCKED** | No git remote configured; nothing pushed |

Everything below that depends on a remote (branch push, PR, protected-
branch merge, tag push, GitHub release) is written as the target process
this repository will follow **once a remote is configured**, not as a
process anyone has executed against this checkout. Wheel build, lock
verification, and sha256 recording are executable today and have been
exercised on this machine. **Local tag signing is separately blocked** —
see [Signing](#signing) — independent of the missing remote.

## Branching

- Work happens on a phase branch, named `phase/<NN>-<slug>` (current
  branch: `phase/00-foundation`). This convention exists so a reviewer
  can tell what phase of work a branch represents without reading its
  diff.
- `main` (or whatever the eventual protected default branch is named
  once a remote exists) is the only branch a release is cut from.
- No commit lands on the protected branch except through a merged pull
  request — there is no direct-push path, by policy (`GIT-001`,
  `GIT-002`: protected history must not be force-pushed).

## Pull requests

**Blocked on GIT-001** until a remote exists. The intended shape once one
does:

1. Push the phase branch:
   ```bash
   git push -u origin phase/<NN>-<slug>
   ```
2. Open a pull request against the protected default branch.
3. The PR description **must identify requirements, tests, and evidence**
   (`GIT-003`) — concretely: which requirement IDs from
   `docs/specifications/SPEC-DREAMY-V2.md` the change addresses, which
   tests cover them, and which gate rows in
   `docs/acceptance/GATE-MATRIX.md` move as a result (including moves
   *into* `BLOCKED` if a change regresses something — the matrix records
   truth, not progress).
4. Review requires at minimum the checks in
   [Pull-request gate](#pull-request-gate) below to be green before
   merge is possible; branch protection (once the remote exists) should
   enforce this mechanically rather than relying on a reviewer to notice
   a red check.

### Pull-request gate

Every PR must pass, in this order, before it is mergeable:

```bash
ruff check src tests          # exit 0
mypy src/dreamy                # exit 0
pytest -q                      # exit 0
tools/verify-locks.sh          # exit 0, if the PR touches pyproject.toml extras or lock files
```

See `CONTRIBUTING.md` for what each of these actually asserts and — this
matters — what a green `pytest -q` does **not** prove.

## Signing

- Tags are signed (`git tag -s`), not merely annotated. An unsigned tag
  on a release commit is not acceptable evidence that the tag was cut by
  who it claims to be from.
- **Blocked on this machine, verified.** Attempting `git tag -s` here
  fails: `error: cannot run gpg: No such file or directory` / `error:
  unable to sign the tag` (exit `128`). No `gpg` binary is on `PATH` and
  no `user.signingkey` is configured in git config. This is a local
  toolchain gap, independent of the missing remote — installing `gpg`
  and configuring a signing key is a prerequisite for tagging on this
  machine even before a remote exists to push the tag to.
- The signing key used must be one already associated with the
  committing identity in whatever remote host is eventually configured,
  so tag verification (`git tag -v`) has something to check against.
- Wheel artifacts themselves are not currently signed (no signing step in
  the build pipeline below); the sha256 of the built wheel is the
  integrity anchor for the artifact, recorded per [Reproducibility](#reproducibility-and-hash-recording).

## Tagging

Once a PR is merged to the protected branch (currently blocked, see
above):

```bash
git tag -s v<version> -m "v<version>"
git push origin v<version>
```

`<version>` matches `project.version` in `pyproject.toml` at the tagged
commit (currently `2.0.0.dev0` — a dev version; no `v` tag should be cut
against a `.devN` version, since that would claim a release for a version
string that explicitly declares itself pre-release).

## Packaging

Build the wheel with `uv`, not bare `python -m build`, so the resolved
build backend is the one pinned in `[build-system].requires`:

```bash
uv build --wheel
```

This was run against this checkout to confirm it works:
```
$ uv build --wheel --out-dir /tmp/dreamy-release-check
Building wheel...
Successfully built /tmp/dreamy-release-check/dreamy-2.0.0.dev0-py3-none-any.whl
$ shasum -a 256 /tmp/dreamy-release-check/dreamy-2.0.0.dev0-py3-none-any.whl
d30da79e9e1f49dc61ac1298ed7c93efc217eb86413e5585e2bd8f0917edf2cc
```
That hash matches the wheel hash `d30da79e…` recorded in `ADR-008`. This
shows only that this machine's checkout produces the same wheel bytes it
produced before — it is **same-machine** repeatability, not
reproducibility. Reproducibility (`G12`) specifically requires a
*second, independent, pristine machine* to reproduce the same hash; that
has not happened and remains **BLOCKED** — see the `G12` discussion
below.

- `[build-system].requires = ["hatchling==1.31.0"]` — pinned exactly, not
  floored. A version range here would let PEP 517 build isolation resolve
  a different hatchling on two machines and produce different wheel
  bytes, defeating reproducibility (`BOOT-001`).
- `packaging/build-requirements.in` must stay byte-identical to that pin.
  `tools/verify-locks.sh` asserts this as one of its checks — it fails
  loudly on a mismatch rather than silently trusting one source over the
  other.
- The wheel packages `src/dreamy` (`[tool.hatch.build.targets.wheel]
  packages = ["src/dreamy"]`); the sdist additionally includes `tests`,
  `docs`, `config`, and `packaging`.

### Verify the build before shipping it

```bash
tools/verify-locks.sh
```

Checks, and fails nonzero on any violation:
1. every declared extra (`agent`, `tui`, `dev`, plus `build`) has a
   committed `config/requirements-<extra>.lock`;
2. every lock file carries `--hash=sha256:` entries (a hashless lock file
   is functionally a version range, and defeats the point of locking);
3. the build backend pin in `pyproject.toml` is exact, not floored;
4. `packaging/build-requirements.in` matches `[build-system].requires`.

This check is **necessary, not sufficient** for reproducibility (gate
`G12`): it proves the locking invariants hold on this one machine. `G12`
itself — a second pristine machine reproducing the same normalized
artifact and package hashes — remains **BLOCKED**, because no second
machine has run this check yet. Passing `verify-locks.sh` here is not
evidence that G12 is satisfied; it is evidence that G12 is *not yet
disproven*.

## Hash-locked dependency strategy

- Runtime dependencies are empty by design (`dependencies = []` in
  `pyproject.toml`) — the deterministic core runs on the standard library
  alone. Everything else is an optional extra.
- Each extra (`agent`, `tui`, `dev`) is pinned to an **exact** top-level
  version in `[project.optional-dependencies]`. Exact pins alone do
  **not** lock the transitive graph — two resolutions of the same
  top-level pin can still pull different transitive versions on
  different days.
- `tools/lock-deps.sh [extra ...]` (default: `agent tui dev`) fully
  resolves each extra's transitive graph with `uv pip compile
  --generate-hashes` (falling back to `pip-tools` if `uv` is absent) and
  writes the result to `config/requirements-<extra>.lock`. It refuses to
  write a lock file that comes back without hashes rather than accepting
  a degraded lock silently.
- The build backend itself is locked the same way, sourced from
  `packaging/build-requirements.in` into
  `config/requirements-build.lock`, because PEP 517 isolated builds
  resolve the backend independently of the runtime extras and would
  otherwise be the one unlocked link in the chain.
- Install any extra reproducibly with:
  ```bash
  PIP_USER=0 pip install --require-hashes -r config/requirements-agent.lock
  ```
  `--require-hashes` refuses to install anything whose downloaded
  artifact hash is not present in the lock file — this is what makes a
  pristine machine (`G1`) and a second machine (`G12`) resolve to
  identical bytes, not merely identical version numbers.

Regenerating a lock file requires network access and a resolver; it is
not part of the normal PR gate (`tools/verify-locks.sh` only checks that
locks exist and are hash-complete, it does not re-resolve them).

## Reproducibility and hash-recording

A release **must** record the wheel's sha256. This is not optional
bookkeeping — `REL-001` and gate `G12` both depend on the built artifact
having a recorded, checkable hash, and `ADR-008` in
`docs/decisions/ADR-008-PRODUCT-NAMING.md` demonstrates why the check
belongs on the artifact rather than the source tree: a source-tree grep
for a superseded product name reported the migration complete while the
built `.whl`'s `dist-info/METADATA` (hoisted from `README.md`) still
carried the old name. The artifact is what a user actually installs; a
release record must describe the artifact, not the tree it was built
from.

```bash
rm -rf dist/
uv build --wheel
shasum -a 256 dist/dreamy-*.whl
```

Clear `dist/` first — a glob against a directory with a stale prior build
will happily hash the wrong file (or several) and record a hash that
does not correspond to the wheel actually being released.

Record the resulting `sha256:<hex>` in the release notes (or GitHub
release body, once a remote exists) alongside the tag it corresponds to.
A release without a recorded wheel hash is not a complete release record,
independent of whether the code inside it is correct.

## What is blocked right now

| Step | Executable today? | Blocker |
|---|---|---|
| `uv build --wheel` | Yes | — |
| `tools/verify-locks.sh` | Yes | — |
| Recording wheel sha256 | Yes | — |
| `git tag -s` locally | **No, on this machine** | `gpg` binary not found (`gpg: command not found`) and no `user.signingkey` configured; verified by attempting it — exit 128 |
| Push branch | **No** | No git remote configured |
| Open pull request | **No** | No git remote configured; depends on push |
| Protected-branch merge | **No** | No git remote configured; depends on PR |
| Push signed tag | **No** | No git remote configured |
| GitHub release | **No** | No git remote configured; no host to publish to |

Configuring a remote is the single unblocking action for the entire
right-hand column. Until then, `GIT-001`, `GIT-002`, `GIT-003`, and
`REL-001` (as far as it depends on a published release) remain
**BLOCKED** in `docs/acceptance/GATE-MATRIX.md`, and no amount of local
process discipline substitutes for that evidence.
