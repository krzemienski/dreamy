# scripts/

Thin developer and release wrappers only. **Not** part of the installed
product.

This directory is currently empty of executables, deliberately. Nothing in the
project has yet needed a wrapper that could not be either a `dreamy`
subcommand or a maintainer tool in `tools/`.

## scripts/ vs tools/ vs src/dreamy/

| Location | Contains | Ships in wheel |
|---|---|---|
| `src/dreamy/` | Product code, reachable via the `dreamy` entry point | yes |
| `tools/` | Maintainer utilities with real logic — generators, lock verification, inventory | no |
| `scripts/` | Thin wrappers around the above, where a stable script path is required | no |

A file belongs in `scripts/` only when **all** of these hold:

1. It cannot reasonably be a `dreamy` subcommand.
2. It is thin — it delegates rather than implements.
3. Something external needs a stable path to it (a CI step, a launchd job, a
   documented one-liner).

If it carries real logic, it belongs in `tools/`. `regen-skills-lock.py`
started here and moved for exactly that reason: at ~170 lines of provenance
recovery and lock generation, it was a tool wearing a wrapper's name.

## Why lock regeneration is not a subcommand

The product *verifies* the skill lock (`dreamy skills verify`); it must never
*rewrite* it. A shipped binary that can regenerate its own pins turns a
tamper-evident record into a self-signed one — "verification failed" would be
one command away from "verification passes". Regeneration therefore stays a
maintainer action, outside the installed surface:

```sh
python3 tools/regen-skills-lock.py --check   # exit 1 if the lock is stale
python3 tools/regen-skills-lock.py           # rewrite after a deliberate change
```

## Constraint

No module under `src/dreamy/` may import from `scripts/` or `tools/`. Runtime
code must not depend on a directory that is absent after installation
[PKG-002].
