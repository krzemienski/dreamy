#!/usr/bin/env bash
# BOOT-001: regenerate fully-resolved, hash-verified lock files for every
# optional extra declared in pyproject.toml.
#
# Exact top-level pins in pyproject.toml do not constrain the transitive graph.
# These lock files do, and `pip install --require-hashes` refuses to install
# anything whose artifact hash is not listed. That is what makes a pristine
# machine (G1) and a second machine (G12) resolve to the same bytes.
#
# Usage:  tools/lock-deps.sh [extra ...]      (default: agent tui dev)
#
# Requires network access and a resolver. Prefers `uv` because it emits
# hashes deterministically; falls back to pip-tools if uv is absent.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="${repo_root}/config"
mkdir -p "${out_dir}"

extras=("$@")
if [ ${#extras[@]} -eq 0 ]; then
  extras=(agent tui dev)
fi

if command -v uv >/dev/null 2>&1; then
  resolver=uv
elif python3 -c 'import piptools' >/dev/null 2>&1; then
  resolver=pip-tools
else
  echo "lock-deps: need either 'uv' or 'pip-tools' installed." >&2
  echo "           pip install uv   # or:  pip install pip-tools" >&2
  exit 2
fi

echo "lock-deps: resolver=${resolver}"
for extra in "${extras[@]}"; do
  target="${out_dir}/requirements-${extra}.lock"
  echo "lock-deps: resolving extra '${extra}' -> ${target}"

  case "${resolver}" in
    uv)
      uv pip compile \
        --extra "${extra}" \
        --generate-hashes \
        --no-header \
        --output-file "${target}" \
        "${repo_root}/pyproject.toml"
      ;;
    pip-tools)
      python3 -m piptools compile \
        --extra "${extra}" \
        --generate-hashes \
        --no-header \
        --output-file "${target}" \
        "${repo_root}/pyproject.toml"
      ;;
  esac

  # Fail loudly rather than committing a lock file with no hashes: a lock
  # without hashes is indistinguishable from a range at install time.
  if ! grep -q -- '--hash=sha256:' "${target}"; then
    echo "lock-deps: ${target} contains no hashes; refusing to accept it." >&2
    exit 3
  fi
done

# BOOT-001: the build backend is resolved at BUILD time, not install time, so
# none of the runtime extras above constrain it. Without this lock a wheel built
# on a second machine can use a different hatchling and produce different bytes,
# which defeats G12. Sourced from packaging/build-requirements.in so the exact
# backend version lives in one place.
build_in="${repo_root}/packaging/build-requirements.in"
build_lock="${out_dir}/requirements-build.lock"
if [ -f "${build_in}" ]; then
  echo "lock-deps: resolving build backend -> ${build_lock}"
  case "${resolver}" in
    uv)
      uv pip compile --generate-hashes --no-header \
        --output-file "${build_lock}" "${build_in}"
      ;;
    pip-tools)
      python3 -m piptools compile --generate-hashes --no-header \
        --output-file "${build_lock}" "${build_in}"
      ;;
  esac
  if ! grep -q -- '--hash=sha256:' "${build_lock}"; then
    echo "lock-deps: ${build_lock} contains no hashes; refusing to accept it." >&2
    exit 3
  fi
else
  echo "lock-deps: missing ${build_in}; build backend is NOT locked." >&2
  exit 4
fi

echo "lock-deps: done. Verify with:"
echo "  pip install --require-hashes -r config/requirements-<extra>.lock"
