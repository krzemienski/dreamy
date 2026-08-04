#!/usr/bin/env bash
# BOOT-001 / G12: assert the dependency-locking invariants actually hold.
#
# Checks, in order:
#   1. every declared extra has a committed lock file
#   2. every lock file carries sha256 hashes (a hashless lock == a range)
#   3. the build backend is pinned exactly, not floored
#   4. packaging/build-requirements.in matches [build-system].requires
#
# Exit 0 = locking invariants hold. Nonzero = do not claim G12.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

fail=0
note() { printf '  %-6s %s\n' "$1" "$2"; }

echo "verify-locks: checking lock coverage"
# Derive the extras from pyproject.toml rather than hard-coding them, so a newly
# declared extra cannot silently escape lock coverage. 'build' is appended
# because the backend lock is sourced from packaging/build-requirements.in and
# is not a [project.optional-dependencies] key.
declared_extras=$(python3 - <<'PY'
import sys, tomllib
with open("pyproject.toml", "rb") as fh:
    data = tomllib.load(fh)
extras = sorted(data.get("project", {}).get("optional-dependencies", {}))
if not extras:
    print("verify-locks: no optional-dependencies declared", file=sys.stderr)
print(" ".join(extras))
PY
) || { note FAIL "could not parse [project.optional-dependencies]"; exit 1; }

for extra in ${declared_extras} build; do
  lock="config/requirements-${extra}.lock"
  if [ ! -f "${lock}" ]; then
    note FAIL "${lock} missing"; fail=1; continue
  fi
  if ! grep -q -- '--hash=sha256:' "${lock}"; then
    note FAIL "${lock} has no sha256 hashes"; fail=1; continue
  fi
  n=$(grep -c -- '--hash=sha256:' "${lock}")
  note ok "${lock} (${n} hashes)"
done

# Inverse direction: a lock file with no matching extra is stale and would
# otherwise sit in config/ looking authoritative.
for lock in config/requirements-*.lock; do
  [ -e "${lock}" ] || continue
  name=$(basename "${lock}" .lock); name=${name#requirements-}
  case " ${declared_extras} build " in
    *" ${name} "*) ;;
    *) note FAIL "${lock} has no matching extra in pyproject.toml (stale)"; fail=1 ;;
  esac
done

echo "verify-locks: checking build backend pin"
requires_line=$(grep -E '^requires *= *\[' pyproject.toml || true)
if printf '%s' "${requires_line}" | grep -qE '[><~!]='; then
  note FAIL "[build-system].requires is not an exact pin: ${requires_line}"
  fail=1
else
  note ok "[build-system].requires is exactly pinned"
fi

pyproject_backend=$(printf '%s' "${requires_line}" \
  | sed -nE 's/.*"([A-Za-z0-9_.-]+==[0-9][^"]*)".*/\1/p')
in_backend=$(grep -E '^[A-Za-z0-9_.-]+==' packaging/build-requirements.in | head -1 || true)

echo "verify-locks: checking backend single-source consistency"
if [ -z "${pyproject_backend}" ] || [ -z "${in_backend}" ]; then
  note FAIL "could not parse backend from pyproject.toml or build-requirements.in"
  fail=1
elif [ "${pyproject_backend}" != "${in_backend}" ]; then
  note FAIL "mismatch: pyproject='${pyproject_backend}' vs in='${in_backend}'"
  fail=1
else
  note ok "backend consistent (${pyproject_backend})"
fi

if [ "${fail}" -ne 0 ]; then
  echo "verify-locks: FAIL — dependency locking is incomplete; G12 not satisfiable." >&2
  exit 1
fi
echo "verify-locks: PASS"
