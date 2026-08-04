#!/usr/bin/env bash
# Publish the current checkout to the public repository, sanitized.
#
# The development history is NOT publishable and never becomes publishable:
# it carries machine-local absolute paths, internal orchestration trees, and
# at one point a 47.6MB session export whose base64 payload decoded to 23,543
# occurrences of the operator's home path. A plain `git push publish` would
# ship all of it. Nothing about that risk is obvious at the moment of pushing,
# which is exactly why this script exists instead of a note in a README.
#
# What it does:
#   1. exports the tracked tree at HEAD (no history)
#   2. strips paths that must never ship, belt-and-braces with .gitignore
#   3. commits a single squashed commit
#   4. runs the decoded-leak sweep and REFUSES to push on any finding
#   5. force-pushes with lease, because the public history is squashed each
#      time and can never fast-forward from the previous release
#
# Usage: tools/release.sh [--dry-run]

set -euo pipefail

REMOTE="${DREAMY_PUBLISH_REMOTE:-publish}"
BRANCH="main"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "release: no '$REMOTE' remote configured" >&2
  exit 2
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "release: working tree is dirty; commit or stash first" >&2
  exit 2
fi

# Paths that must never reach the public repository. Kept here rather than
# relying on .gitignore alone: a path can be tracked from before an ignore
# rule existed, and gitignore does not untrack retroactively.
EXCLUDE=(
  plans evidence e2e-evidence banks
  .omp .omo .omc .shannon .crucible .campaign .claude
  TASK.md SPEC-dreamy.md SPEC-addendum-prompts-p1p2-tui.md
)

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

echo "release: exporting tracked tree at $(git rev-parse --short HEAD)"
git archive HEAD | tar -x -C "$staging"

for path in "${EXCLUDE[@]}"; do
  rm -rf "${staging:?}/${path}"
done
# Session exports are generated artifacts and can be arbitrarily large.
find "$staging" -maxdepth 1 -name 'omp-session-*.html' -delete

cd "$staging"
git init -q -b "$BRANCH"
git add -A
git \
  -c user.name="$(git -C "$repo_root" config user.name)" \
  -c user.email="$(git -C "$repo_root" config user.email)" \
  commit -q -m "feat: dreamy — cross-harness coding-session reconciler

Read-only pipeline that ingests coding sessions from six agent harnesses,
attributes them to projects, correlates work across harnesses, analyzes
continuity deterministically plus an optional agent layer, compiles prompt
artifacts, and presents a Textual TUI and a loopback web dashboard.

Published from a squashed history: the development history carries
machine-local paths and internal workflow artifacts that do not belong in a
public repository."

echo "release: $(git ls-files | wc -l | tr -d ' ') files staged"

# The gate. A sweep that only greps raw bytes reports clean on base64 and
# separator-encoded leaks — both of which have actually shipped here — so the
# sweep decodes before matching.
sweep="$repo_root/tools/leak_sweep.py"
if [[ ! -f "$sweep" ]]; then
  echo "release: leak sweep missing at $sweep; refusing to push" >&2
  exit 2
fi

echo "release: running decoded leak sweep"
allow="$repo_root/tools/release-allowed-secrets.txt"
if [[ ! -f "$allow" ]]; then
  echo "release: allowlist missing at $allow; refusing to push" >&2
  exit 2
fi
if ! python3 "$sweep" "$staging" --allow-file "$allow"; then
  echo "release: REFUSING TO PUSH — leak sweep found disclosures above" >&2
  exit 1
fi

# Target validation runs BEFORE the dry-run exit. A dry run that skips it
# proves nothing about the push that follows, which is the one thing a
# rehearsal exists to check.
remote_url="$(git -C "$repo_root" remote get-url "$REMOTE")"

# Remote allowlist. This script force-pushes a squashed history; pointed at
# the wrong repository it destroys that repository's history. Refuse anything
# that is not an explicit https GitHub URL, and require the caller to name the
# expected owner/repo so a mistyped or hijacked remote cannot be published to.
expected="${DREAMY_PUBLISH_REPO:-}"
if [[ -z "$expected" ]]; then
  echo "release: set DREAMY_PUBLISH_REPO=owner/repo to confirm the target" >&2
  exit 2
fi
if [[ "$remote_url" != "https://github.com/${expected}.git" && \
      "$remote_url" != "https://github.com/${expected}" ]]; then
  echo "release: remote '$REMOTE' is $remote_url, expected github.com/$expected" >&2
  exit 2
fi

# Build and test the STAGED tree, not the working checkout. They differ —
# paths are stripped and history squashed — and the published artifact is the
# one that has to install and pass, on a machine that has never seen this
# repository. Gates run before the dry-run exit so a rehearsal exercises them.
echo "release: building and testing the staged tree"
python3 -m venv .release-venv >/dev/null
PIP_USER=0 ./.release-venv/bin/pip -q install -e '.[dev,agent]' >/dev/null
PIP_USER=0 ./.release-venv/bin/ruff check src/ tests/
PIP_USER=0 ./.release-venv/bin/mypy src/dreamy >/dev/null
# `acceptance` gates measure an operator-local evidence corpus produced by the
# development checkout. The staged tree is a different workspace, so they
# compare unrelated things and fail on the mismatch — the exact case CI never
# hits, because a clean runner has no corpus and they skip. Deselected here
# for the same reason: this measures the artifact, not the workstation.
PIP_USER=0 ./.release-venv/bin/python -m pytest -q -m "not acceptance"
rm -rf .release-venv
echo "release: gates passed on the staged tree"

# The gates just ran an editable install and a test suite inside the staged
# tree. Both can write: __pycache__, .pytest_cache, egg-info, coverage data,
# stray fixtures. Whatever they left is about to be published, so the tree is
# re-checked and re-swept rather than assumed unchanged since the first sweep.
if [[ -n "$(git status --porcelain)" ]]; then
  echo "release: build/test gates left artifacts in the staged tree:" >&2
  git status --porcelain >&2
  echo "release: refusing to publish an unexamined tree" >&2
  exit 1
fi

echo "release: re-sweeping after gates"
if ! python3 "$sweep" "$staging" --allow-file "$allow"; then
  echo "release: REFUSING TO PUSH — post-gate sweep found disclosures" >&2
  exit 1
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "release: dry run — target and gates validated, not pushing."
  echo "release: staged tree at $staging"
  trap - EXIT
  exit 0
fi

git remote add origin "$remote_url"
# Branch absence must be PROVEN, not inferred from a failed fetch. A network
# outage, an expired token, or a typo'd remote all fail the same way, and
# treating that as "branch does not exist yet" turns a transient error into an
# unguarded create-push against an unknown remote state.
set +e
git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1
ls_status=$?
set -e
case "$ls_status" in
  0)
    git fetch -q origin "$BRANCH"
    remote_sha="$(git rev-parse FETCH_HEAD)"
    echo "release: remote $BRANCH is at ${remote_sha:0:12}"
    # --force-with-lease needs a remote-tracking ref to compare against, and a
    # fresh staging repo has none: the lease would be vacuously satisfied and
    # degrade silently to a plain --force. The explicit expected-value form is
    # the only one that actually holds here.
    git push --force-with-lease="refs/heads/${BRANCH}:${remote_sha}" origin "HEAD:${BRANCH}"
    ;;
  2)
    echo "release: remote $BRANCH does not exist; creating it"
    git push origin "HEAD:${BRANCH}"
    ;;
  *)
    echo "release: cannot reach $remote_url (ls-remote exit $ls_status); refusing" >&2
    exit 2
    ;;
esac
echo "release: pushed to $remote_url ($BRANCH)"
