# Security Policy

## Threat model

dreamy is a **read-only** reconciler over local coding-agent session stores
(`~/.claude/projects`, `~/.codex`, `~/.omp`, `~/.pi`, `~/.9router`,
`~/.local/share/opencode`, and similar). Those stores hold raw transcript
content — prompts, tool output, file paths, and occasionally credentials a
user or agent pasted into a session by accident. That makes the credible
threats:

- **Sensitive prose disclosure.** Session transcripts can contain anything
  the user typed or an agent printed — API responses, internal file
  contents, PII. dreamy ingests, correlates, and compiles excerpts from this
  material into its own store and reports. Anyone who can read dreamy's
  output can read a summarized/excerpted view of the underlying sessions.
- **Secret leakage into dreamy's own state.** If a live credential appears
  in a transcript, a naive ingestion pipeline would happily copy it into
  `state.db`, into compiled prompt artifacts, or into a shareable bundle —
  turning a single accidental paste into two persistent copies.
- **Filesystem disclosure via shareable output.** Bundles (`dreamy bundle`)
  are explicitly meant to leave the machine that produced them. An absolute
  path under `$HOME` embedded in a bundle discloses the operator's account
  name and local directory layout to whoever receives the file.
- **Write access to source stores.** dreamy is not supposed to write to,
  mutate, or delete anything under a connector's source directory — only
  read from it.

dreamy is **not** a network service, does not accept inbound connections,
and does not transmit data anywhere by default. The realistic attack surface
is local: what dreamy reads from disk, what it writes back to disk, and what
a user hands to someone else via a bundle or the web dashboard (which is
loopback-only — see `docs/operations/*` for the exact bind address if you
need to verify this yourself).

## What dreamy guarantees, and where that's enforced

The following claims are backed by code I read while writing this policy,
not by hearsay from other docs:

- **Live credentials are never persisted.** `src/dreamy/redact.py` reads
  known secret sources (`~/.9router/auth/cli-secret` and a fixed set of
  environment variables — see `_SECRET_SOURCE_FILES` /
  `_SECRET_ENV_VARS`) once per process, then strips those *exact byte
  values* from any text before it's written anywhere. This is deliberately
  not pattern matching for the general case: the module's own comments
  document a real incident (defect D11) where a 64-character hex router
  token slipped past every regex, because unanchored hex is
  indistinguishable from the SHA-256 content fingerprints the store
  legitimately holds everywhere. The fix was exact-value matching against
  known-secret sources, applied via `redact()` / `redact_dict()` /
  `redact_value()` / `redact_text()` at every ingestion boundary
  (`correlate.py`, each connector in `src/dreamy/connectors/`,
  `logging_util.py`). A separate, shape-based canary
  (`sk-ant-test-PLANTED-SECRET-…`, `CANARY` in `redact.py`) exists purely
  for acceptance testing of the redaction path itself — it is not part of
  the live-secret guarantee.
- **A dedicated doctor check enforces the no-persisted-secrets guarantee.**
  `check_canary_absent` in `src/dreamy/doctor.py` (check id `N4-1`,
  "N4 zero secrets persisted") scans every artifact dreamy writes under its
  output directory, plus every scrubbable column of `state.db`, for the
  canary value, and fails if any occurrence survives. `redact.py` also
  exposes `scrub_database()`, which re-redacts and overwrites already-written
  free-text columns (`SCRUBBABLE_COLUMNS` in `redact.py`) — the safety net
  for content that reached disk before a redaction gap was closed.
- **Bundles are scrubbed of the operator's home path before export.**
  `src/dreamy/bundle.py` rewrites every rendered path through
  `_portable_path()` / `_portable_text()`, replacing `str(Path.home())`
  with `~` before an export is written. `export_project_bundle()` (also in
  `bundle.py`) then verifies the rewrite actually held: it re-scans the
  fully rendered HTML for `contains_canary(html_doc)`, for the live auth
  token value, and for `str(Path.home())` itself, and raises rather than
  writes the bundle if any of the three is still present. `doctor.py`'s
  `check_bundle_hygiene` (check id `R14-1`) independently exports a real
  bundle for a real project during a doctor run and re-checks the same three
  conditions plus external asset references, so the guarantee is exercised
  by two independent code paths, not just the exporter's own belief in
  itself.
- **Source stores are read-only.** dreamy's connectors
  (`src/dreamy/connectors/{claude,codex,omp_pi,opencode,router}.py`) open
  each harness's store for reading only; `doctor.py`'s
  `check_repo_read_only` (check id `R9-1`) is part of the standard `dreamy
  doctor` run and specifically verifies this property rather than assuming
  it. dreamy never writes to, moves, or deletes anything inside a
  connector's `default_source_path()`.

Run `dreamy doctor` yourself to see these checks execute live; a healthy
install reports all of N4, R14, and R9 as `PASS`.

## What dreamy does *not* protect against

- Content that never reaches a known-secret source or a shape the redactor
  recognizes (arbitrary novel secret formats) can still be excerpted into
  dreamy's store and reports — this is a best-effort, evidence-driven
  guard, not a general-purpose secret scanner. Treat anything dreamy
  compiles as carrying the same sensitivity as the transcripts it was
  compiled from.
- dreamy's own output directory and `state.db` are not encrypted at rest.
  Filesystem permissions on your account are your protection for data at
  rest, same as the source stores dreamy reads.
- Optional extras (`agent`, `tui`) pull in third-party dependencies
  (`claude-agent-sdk`, `textual`) pinned in `pyproject.toml`. Those
  dependencies' own security posture is outside this policy's scope; report
  issues in them upstream.

## Supported versions

dreamy is pre-1.0 (`version = "2.0.0.dev0"` in `pyproject.toml`) and does
not yet maintain parallel release branches. Security fixes land on the
default branch and are backported only if a maintained release branch
exists at the time. Until a stable 1.0 release, **run the latest commit on
the default branch** to get security fixes — there is no older version
receiving separate patches.

## Reporting a vulnerability

Please do **not** open a public GitHub issue for a suspected security
vulnerability.

Use GitHub's private vulnerability reporting instead:
[github.com/krzemienski/dreamy/security/advisories/new](https://github.com/krzemienski/dreamy/security/advisories/new)
(**Security** tab → **Report a vulnerability**). This opens a private
advisory visible only to you and the maintainers until a fix is ready and
you agree to disclose.

Include, where you can:

- The command or code path that triggers the issue (`dreamy <subcommand>
  ...`), with any sensitive arguments redacted.
- What you expected vs. what happened, including whether a secret, home
  path, or session content ended up somewhere it shouldn't have (a
  `state.db` column, a compiled prompt artifact, a bundle, stdout/stderr).
- Output of `dreamy doctor`, if it runs at all.
- Your dreamy version, Python version, and OS.

You should expect an initial response within a few days. There is no formal
bug-bounty program; credit will be given in the advisory and release notes
unless you ask otherwise.
