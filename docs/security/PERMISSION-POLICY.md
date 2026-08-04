# PERMISSION-POLICY

Requirements: `SAFE-001`, `SAFE-003`, `SDK-004`, `SDK-005`

The default-deny tool and path policy for every agent call Dreamy makes.
This document is written to be implementable and testable — every rule below
names the exact field, the exact value, and the exact file where it must be
enforced. Where the policy is not yet enforced in code, that is stated
plainly rather than implied.

See `THREAT-MODEL.md` for the reasoning behind each control and
`docs/research/SDK-CAPABILITY-MATRIX.md` for the underlying SDK evidence.

## Current implementation status (read this first)

`src/dreamy/agent_sdk.py::call_claude` constructs its `ClaudeAgentOptions`
with exactly these fields today:

```python
options = ClaudeAgentOptions(
    model=model,
    env=base_env,
    cwd=cwd,
    max_budget_usd=call_budget,
    cli_path=_cli_path,
)
```

**No `permission_mode`, `allowed_tools`, `disallowed_tools`, or `hooks` is
set.** An AST scan confirms no code path in `src/dreamy/` constructs a call that
*requests* `Write`, `Edit`, or unrestricted `Bash`.

**That is not the same as proving the SDK would refuse a write, and `SAFE-001`
is therefore UNVERIFIED.** The scan establishes what Dreamy asks for. It says
nothing about what the CLI grants by default when `allowed_tools` is empty —
the installed defaults are `permission_mode=None`, `allowed_tools=[]`,
`disallowed_tools=[]`, `can_use_tool=None`, `hooks=None`, and an empty
collection does not self-evidently mean "no tools". Whether that yields
deny-all or the full built-in set is a runtime question, and reading a
dataclass cannot answer it.

What holds today: Dreamy does not request write capability, and no `--apply`
flag, TUI action, or config key enables repository mutation. What is NOT
established: that a write attempt would be denied if the model made one.

A conformance test exists at `tests/sdk_conformance/test_mutation_denial.py`
— but it does not close `SAFE-001` yet, for three independent reasons,
each stated here rather than left implicit:

1. **It is opt-in and currently skipped.** Gated behind
   `DREAMY_RUN_SDK_MUTATION_TEST=1`; without that env var set, every test in
   the file reports `skipped`, which evidences nothing about enforcement.
2. **Its deny-configuration case is stale against this document's revised
   target policy.** `test_specified_deny_configuration_observes_no_attempt`
   constructs `ClaudeAgentOptions(permission_mode="dontAsk",
   allowed_tools=["Read", "Grep", "Glob"], disallowed_tools=["Write",
   "Edit", "NotebookEdit", "Bash"], ...)` — but this document's target
   configuration (§"Allowed built-ins" above) is `tools=[]` plus the
   bare-name `disallowed_tools` deny list, not a three-tool
   `allowed_tools` entry, because no current agent needs `Read`/`Grep`/
   `Glob`, and `allowed_tools` does not restrict availability regardless
   (confirmed against the installed SDK's own docstring — see §"Allowed
   built-ins"). **This test does not exercise the policy this document
   actually specifies**, and must be updated to test `tools=[]` before it
   can serve as evidence for this document's actual target config.
   This item remains OPEN.
3. **Its expected outcome contradicted the claim it was testing. Test
   mechanics CORRECTED; the underlying question remains open.** The test
   asserted `outcome.startswith("DENIED_BY_PERMISSION")` — it expected the
   model to attempt a forbidden tool and be refused. But §"Why
   `disallowed_tools` must use bare names", sourced from
   `SDK-CAPABILITY-MATRIX.md`, states that a bare name like `"Bash"`
   **removes the tool from the model's context entirely**, so the model could
   never attempt it — reported by the classifier as `NOT_ATTEMPTED`. The
   old assertion contradicted this document's own documented removal
   semantics, and now expects `NOT_ATTEMPTED`. Note the correction is to the
   assertion's consistency with the documented contract — a passing
   `NOT_ATTEMPTED` still does not prove removal caused it.

   That is a fix to the test, NOT an upgrade to `SAFE-001`. Three distinct
   evidence levels, none of which may stand in for another:

   * **Shipping configuration — what `SAFE-001` is actually about.** Production
     `call_claude` sets NO permission configuration: no `tools`, no
     `allowed_tools`, no `disallowed_tools`, no hooks. An AST scan shows
     Dreamy never requests `Write`/`Edit`/`Bash`, which establishes intent,
     not enforcement. `SAFE-001` remains **UNVERIFIED**.
   * **Bare-deny configuration, opt-in test.** Observes that no forbidden tool
     was attempted and no file was written. It does NOT establish
     context-removal: `NOT_ATTEMPTED` is equally consistent with the model
     declining, and nothing in the message stream distinguishes "not offered"
     from "offered and not chosen". `SDK-CAPABILITY-MATRIX.md`'s bare-name
     claim is therefore still not confirmed by this run.
   * **Hook test — the only enforcement evidence.**
     `test_each_forbidden_tool_is_refused` GRANTS the tool so an attempt is
     possible, then denies it from a `PreToolUse` hook and asserts
     `DENIED_BY_PERMISSION`. It exercises a configuration production does not
     yet use.

   Confirming context-removal directly still requires (b): a test that
   inspects tool *availability* — whatever the SDK exposes as the model's
   offered-tool set at session start — rather than inferring removal from an
   attempt that, if the claim holds, should never happen.

Even once these are resolved, the test only classifies the observed
outcome — it prompts the model to create a file at an out-of-repo scratch
path and distinguishes `DENIED_BY_PERMISSION`, `NOT_ATTEMPTED`, and
`TOOL_ERROR` (the call failed for some other reason, possibly a bad path)
via `ResultMessage.permission_denials` plus `ToolUseBlock`/`ToolResultBlock`
pairing. Until all three items above are resolved, citing this test as
evidence that `SAFE-001` is closed would be exactly the kind of overclaim
this document exists to prevent.

A parametrized per-tool case (`Write`, `Edit`, `Bash`) also exists in the
same file, skipped under the same flag and subject to the same three
caveats above — `Bash` matters most among them, since it reaches the same
mutation by a different route and could in principle be granted while
`Write` is denied.

The rest of this document specifies the policy that closes the gap; none of it
is wired into `call_claude` unless explicitly marked IMPLEMENTED.

## Tool policy

### Allowed built-ins — `tools=[]`, confirmed against the hash-locked SDK install

No current agent call site *requires* a built-in tool. Verified by
inspection: `research.py`, `teacher.py`, `friends.py`, and `compiler.py`
all build their prompts by inlining evidence directly into the `prompt=`
string passed to `call_claude` — none of the four points the model at a
filesystem path and asks it to open one.

**The offered/context tool set and the auto-approval tool set are two
separate fields, and this document previously conflated them.** Confirmed
directly against the docstrings in the hash-locked
`claude-agent-sdk==0.2.128` install (`/tmp/dreamy-agent`, built per
`SDK-VERSION-LOCK.md`'s "Which environment to verify in" — an earlier
draft inspected the ambient `python3`'s unrelated `0.1.68` install and is
corrected here):

```
tools: list[str] | ToolsPreset | None
    "Specify the base set of available built-in tools.
     - list[str] — Specific tool names.
     - [] (empty list) — Disable all built-in tools.
     - {"type": "preset", "preset": "claude_code"} — Use all default tools.
     To restrict which tools the model may call without being prompted,
     use allowed_tools instead."

allowed_tools: list[str]
    "Tool names that are auto-allowed without prompting for permission.
     These tools execute automatically without asking the user for
     approval. To restrict which tools are available at all, use tools."

disallowed_tools: list[str]
    "Tool names that are disallowed. These tools are removed from the
     model's context and cannot be used, even if they would otherwise be
     allowed."
```

`tools=[]` — **not `allowed_tools=[]`** — is therefore the offered-set
restriction mechanism, per the SDK's own documented contract. Dreamy's
target configuration is

```python
tools = []
```

`allowed_tools` is left unset (its default is an empty list, which the
SDK's own docstring says restricts nothing about availability — only
whether an already-available tool prompts before running); `disallowed_tools`
remains the bare-name deny list documented below as defense in depth, since
`tools=[]` denying availability and `disallowed_tools` removing named tools
"from the model's context" are two independently-documented mechanisms, not
one relying on the other.

**Status:** SPECIFIED. `call_claude` sets none of `tools`, `allowed_tools`,
`disallowed_tools`, or `permission_mode` today.

### If a future capability needs `Read`/`Grep`/`Glob`: path enforcement must live in `PreToolUse`, not `can_use_tool`

This correction matters enough to state plainly: an earlier version of
this document proposed enforcing a path allowlist for `Read`/`Grep`/`Glob`
via a `can_use_tool` callback. That is unenforceable by this document's own
stated permission-evaluation order (§"`allowed_tools` as the primary
mechanism" below): **`can_use_tool` is not invoked for a call already
auto-approved by `allowed_tools`.** If `Read` is on `allowed_tools`, every
`Read` call is auto-approved before `can_use_tool` ever runs — the
callback the earlier text relied on would never fire for the exact calls
it needed to gate. Self-contradictory, and corrected here rather than left
standing.

The permission-evaluation order —

```
Hooks → Deny rules → Ask rules → Permission mode → Allow rules → can_use_tool
```

— places `Hooks` first, ahead of `Allow rules`. A `PreToolUse` hook,
**not** `can_use_tool`, is therefore the only mechanism in this ordering
that can inspect and reject a `Read`/`Grep`/`Glob` invocation's target path
before an allow rule auto-approves the call. If `Read`/`Grep`/`Glob` are
ever granted, the corresponding `PreToolUse` hook must:

1. Read the tool call's target path from `input_data` (the hook's own
   payload, not a separately-tracked value).
2. Canonicalize it (`.resolve()`), rejecting a symlink by walking path
   components before resolving — the same ordering `acceptance.py` uses,
   documented in the Path canonicalization rules below.
3. Deny unless the canonicalized path falls under one specific allowed
   root: `<output_dir>/reports/latest/projects/<slug>/evidence/` — the
   per-artifact evidence directory the compiler agent itself writes,
   already redacted, already Dreamy-owned. No other root is ever legitimate
   for a model-initiated read, because every agent's *actual* evidence
   today arrives inline in the prompt text, not via a file the model reads
   itself — inlined text is not a filesystem root and is not part of this
   allowlist; it is simply what the prompt already contains.

**Explicitly denied, and to be asserted by the regression test in the
"Testing this policy" section below, not merely stated:**

| Path | Why it must be denied |
|---|---|
| Any managed target repository (the git checkouts sessions were about) | `SAFE-001` — the asset the entire read-only guarantee protects |
| Any harness store (`~/.claude/projects`, `~/.codex`, `~/.omp`, OpenCode's SQLite path, Nine Router's history) | Dreamy's own input, already ingested and redacted before any prompt is built (§1/§2 of `THREAT-MODEL.md`); no legitimate reason for the model to re-read it unredacted |
| `~/.9router/auth/cli-secret` and every location in `redact.py::_SECRET_SOURCE_FILES` / the paths `_SECRET_ENV_VARS` are sourced from | The exact secret sources `redact.py` reads to know what to strip — granting the model read access to the source defeats the redaction it exists to enforce |
| `~/.ssh`, `/etc`, and any path outside the one allowed root above | Default-deny: nothing is permitted unless explicitly listed |

**Status:** SPECIFIED. `tools=[]` is the correct target configuration
today (no agent needs file tools), and `call_claude` does not currently
set `tools`, `allowed_tools`, or `permission_mode` at all — meaning the
CLI's own default for an unset `tools` field governs, which
`PERMISSION-POLICY.md`'s "Current implementation status" section above
flags as an open, **unverified** question, not an assumed-safe empty set.
If `Read`/`Grep`/`Glob` are added in the future, they MUST ship together
with the `PreToolUse` path-enforcing hook above in the same change — never
as a bare `tools` addition on its own.

### Denied built-ins

| Tool | Rationale |
|---|---|
| `Write` | Never permitted. This is the tool that would violate `SAFE-001` directly. |
| `Edit` | Same as `Write` — file mutation. |
| `NotebookEdit` | Same category as `Edit`, for notebook files. |
| `Bash` | **Denied as a bare name, not a scoped rule.** See below. |

### Why `disallowed_tools` must use bare names

This is the single most consequential formatting detail in this policy, and
getting it wrong silently reopens the exact hole it exists to close.

`disallowed_tools` entries are matched by the SDK as either a **bare tool
name** or a **scoped pattern**:

- `"Bash"` (bare) — removes the `Bash` tool from the model's context
  entirely. The model cannot invoke it under any argument.
- `"Bash(rm *)"` (scoped) — denies only calls matching that specific
  pattern. **The bare `Bash` tool remains available for every other
  invocation.** A scoped deny rule is an allow rule with one carve-out, not
  a deny rule.

A policy author reasoning "I'll deny the dangerous `Bash` invocations" and
writing `disallowed_tools=["Bash(rm *)", "Bash(git push*)", ...]` has, in
practice, **allowed** `Bash` — every command not matching one of those
specific patterns still executes. Dreamy's `disallowed_tools` list MUST
consist entirely of bare names for any tool that is fully prohibited:

```python
disallowed_tools = ["Write", "Edit", "NotebookEdit", "Bash"]
```

Scoped patterns are appropriate only for tools that are *conditionally*
allowed — a category Dreamy currently has zero members of. If a future
capability needs `Bash` for a narrow, provably read-only purpose (e.g. `git
status`), that capability must be built as a dedicated typed tool (per
`SDK-CAPABILITY-MATRIX.md` disposition #10), not as a scoped carve-out on
the general-purpose `Bash` tool — a scoped carve-out cannot be statically
verified to be safe against every future model output, while a dedicated
tool's implementation can be read in full.

**Status:** SPECIFIED. `call_claude` does not currently set
`disallowed_tools` at all; the rule above is the target configuration.

### `tools=[]` as the primary mechanism, `disallowed_tools` and `permission_mode` as independent layers

Per the documented permission evaluation order:

```
Hooks → Deny rules → Ask rules → Permission mode → Allow rules → can_use_tool
```

`tools=[]` is the primary boundary for Dreamy's purpose, per its own
docstring in the installed SDK: it disables all built-in tools outright,
rather than merely skipping a prompt for tools that remain available.
Given today's agents need no file tools at all (§"Allowed built-ins"
above), Dreamy's target configuration is:

```python
options = ClaudeAgentOptions(
    ...,
    tools=[],
    disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"],
    permission_mode="dontAsk",
)
```

The overlap between `tools=[]` and the bare-name `disallowed_tools`
entries is deliberate defense in depth, not redundancy on a single
mechanism: `tools=[]` denies availability outright per its own contract;
`disallowed_tools` independently removes named tools "from the model's
context" per its own contract. Either alone should suffice given their
documented behavior; both together guard against a future edit that
widens one without re-examining the other. If `Read`/`Grep`/`Glob` are
later added to `tools`, they must ship together with the `PreToolUse`
path-enforcing hook in the combined example below — never on their own.

`can_use_tool` is **not** invoked for a call already auto-approved by
`allowed_tools`, an allow rule, or `permission_mode`. A callback registered
alongside an allow rule for the same tool never runs — the allow rule wins
first in evaluation order. This is exactly why the earlier `can_use_tool`
proposal for path scoping was wrong and has been replaced with the
`PreToolUse` hook below.

**Status:** SPECIFIED, not yet implemented.

### `permission_mode`

`permission_mode="dontAsk"` is the correct mode for Dreamy's unattended,
non-interactive analysis calls. Verified present in the installed build's
`PermissionMode` literal set: `default`, `acceptEdits`, `plan`,
`bypassPermissions`, `dontAsk`, `auto`.

**Never** use:

- `bypassPermissions` — documented to **ignore `allowed_tools` entirely**.
  Setting this mode would make the entire tool allow list above a no-op.
- `acceptEdits` — auto-approves file writes and destructive `Bash`, which is
  the exact behavior `SAFE-001` forbids.

**Status:** SPECIFIED, not yet implemented — `call_claude` does not
currently set `permission_mode`.

### `PreToolUse` hook backstop

Hooks run **first** in the evaluation order, ahead of deny rules, and apply
**even under `bypassPermissions`** — the one control that is not defeated
by a permission-mode misconfiguration. A `PreToolUse` hook is required as an
independent second check on top of `allowed_tools`/`disallowed_tools`, not
as a replacement for them. Combined mutation-tool denial and (for the day
`Read`/`Grep`/`Glob` are added) path-scoped enforcement live in one hook,
so there is exactly one place this policy is enforced at the hook layer:

**This is pseudocode, not a verified implementation** — the exact
`PreToolUse` `input_data` payload shape for `Grep`/`Glob` (which field
carries the search/glob root: `path`, `file_path`, `pattern`, something
else) has not been confirmed against the installed SDK build in this
review, and must be checked against real `input_data` from a captured
`PreToolUse` invocation before this is implemented — not assumed from the
field name used for `Read`. The contract this pseudocode must satisfy:

```python
_MUTATION_TOOLS = {"Write", "Edit", "NotebookEdit", "Bash"}
_READ_TOOLS = {"Read", "Grep", "Glob"}
_ALLOWED_READ_ROOT = None  # set to the per-artifact evidence dir, resolved,
                           # if/when Read/Grep/Glob are ever added to
                           # the `tools` allowlist; None denies all three

def _target_path(tool_name: str, tool_input: dict) -> Path | None:
    # PLACEHOLDER: verify the actual key(s) `tool_input` carries per tool
    # against a real captured PreToolUse payload before implementing.
    # Read uses a file path; Grep/Glob's root-path key is UNCONFIRMED here.
    raw = tool_input.get("file_path") or tool_input.get("path")
    return Path(raw) if raw else None

def _is_within_allowed_root(target: Path, root: Path) -> bool:
    # Symlink rejection walks TARGET's own components relative to a known
    # safe root, checking is_symlink() at each step before any resolve() —
    # the same ordering acceptance.py uses (see Path canonicalization
    # rules below). It does NOT join target's parts onto root; a target
    # outside root by definition has no valid decomposition relative to
    # root's own tree, so containment is decided by resolve()+parents
    # after the walk confirms no traversed prefix of target is a symlink.
    try:
        for ancestor in (target, *target.parents):
            if ancestor.is_symlink():
                return False
        resolved = target.resolve()
        resolved_root = root.resolve()
        return resolved == resolved_root or resolved_root in resolved.parents
    except OSError:
        return False

async def deny_mutation_and_scope_reads(input_data, tool_use_id, context):
    tool_name = input_data.get("tool_name")
    if tool_name in _MUTATION_TOOLS:
        return {"decision": "deny", "reason": "Dreamy is read-only"}
    if tool_name in _READ_TOOLS:
        target = _target_path(tool_name, input_data.get("tool_input", {}))
        if (
            _ALLOWED_READ_ROOT is None
            or target is None
            or not _is_within_allowed_root(target, _ALLOWED_READ_ROOT)
        ):
            return {"decision": "deny", "reason": "path outside allowed evidence root"}
    return {}
```

Registered via
`hooks={"PreToolUse": [HookMatcher(hooks=[deny_mutation_and_scope_reads])]}`.

This hook must be static: it hardcodes the same tool set as
`disallowed_tools` above and is not itself configurable at runtime, so a
config error elsewhere cannot silently widen it. `_ALLOWED_READ_ROOT`
stays `None` (denying every `Read`/`Grep`/`Glob` call outright) until the
day a future capability actually needs one of those tools and sets it
explicitly — it must never default to something permissive.

**Status:** SPECIFIED, not yet implemented. No hooks of any kind are
currently registered in `agent_sdk.py`.


## Path canonicalization rules

These rules govern every filesystem path Dreamy resolves from data it does
not fully control: a decoded harness project directory, an
acceptance-manifest-declared artifact, or a cited evidence path.

1. **Resolve before comparing containment, never after.** Two paths being
   compared for "is A inside B" must both be canonicalized
   (`Path.expanduser().resolve()`) before the comparison, or a relative
   segment (`..`) or a symlink can defeat a naive string-prefix check.
   Implemented for run-root containment: `acceptance.py::validate_run_manifest`
   resolves both `root` and `runs_base` before checking `base not in
   root.parents`. `runs_base` itself defaults to a `HOME`-derived value
   (`ACCEPTANCE_RUNS_BASE`); this is ordinary configuration behavior, not
   a defended-against attack surface, since the manifest cannot set
   `HOME` and whoever does control the validating process's own
   environment already controls the verification outcome directly.

2. **Symlink rejection happens on the un-resolved path, before
   `.resolve()` is called on it — never after.** This ordering is not
   interchangeable. Calling `.resolve()` first silently follows a symlink
   and hands downstream code a plain path indistinguishable from a
   non-symlinked one; by the time containment is checked, the fact that a
   link was traversed is already lost. The correct pattern, implemented in
   `acceptance.py`'s artifact-resolution loop, is to walk each path
   component from a known-safe root and check `is_symlink()` at every step
   *before* any `.resolve()` call is made on the full path:

   ```python
   walked = root
   for part in Path(rel).parts:
       walked = walked / part
       if walked.is_symlink():
           raise ManifestError(f"path traverses a symlink at {walked}")
   target = unresolved.resolve()  # only after the walk finds nothing
   ```

3. **Containment checks must walk components, not just filter `.parents`.**
   A predicate that only checks `root not in target.parents` misses the
   case where the symlinked entity is itself a *direct child* of `root` —
   that child's own `.parent` *is* `root`, so a parents-based filter alone
   passes it. The component-walk in rule 2 covers this case; a
   parents-only check does not.

4. **Absolute paths in a manifest-declared relative field are rejected
   outright**, before any join or resolve is attempted:
   `if Path(rel).is_absolute(): raise ManifestError(...)`. This prevents a
   caller from bypassing the run-root-relative model entirely by supplying
   `/etc/passwd` where a relative path was expected.

5. **Project-directory decoding never guesses across a hyphen boundary
   using string manipulation alone, for the matched portion of the
   walk — and its unresolved-tail fallback now rejects a traversal
   attempt instead of returning one.** `pathdecode.py` performs a
   filesystem-aware greedy walk for the part of a directory name it can
   match against real children on disk, rather than a blind
   `replace('-', '/')`, which would be wrong for any real directory name
   containing a hyphen (e.g. `yt-transition-shorts-detector`). When the
   walk cannot match the full name, the leftover ("unresolved tail") is
   checked and, if it equals `.`/`..` or contains an embedded `/` or `\`,
   `_decode_under` raises `ValueError` rather than joining it as a path
   component — confirmed by direct execution, not inference:

   ```python
   >>> pathdecode.decode_claude_project_dir('-tmp-project-..')
   ValueError: refusing to decode malformed directory segment '..': ...
   >>> pathdecode.decode_omp_session_dir('-..', home)
   ValueError: refusing to decode malformed directory segment '..': ...
   ```

   A separate `_is_unsafe_cwd()` helper independently rejects any
   resulting cwd string containing a `..` segment, a non-`/`-rooted path,
   or embedded NUL/backslash — a second layer, not the sole check. Neither
   `changes.py` (the git subprocess `cwd`) nor `run.py` (`Path.is_dir()`
   checks) can receive an escaping path from the decoder, because the
   decoder itself refuses to produce one.

   The connector layer that calls the decoder is also correctly wired:
   `omp_pi.py`'s call site catches the `ValueError` above the same way
   `claude.py`'s equivalent call site does, falling back to an empty
   `cwd` with a warning rather than aborting the connector's scan.
   `tests/integration/test_connector_hostile_dirs.py` covers all three
   connectors' handling of a hostile directory name.

6. **Evidence-citation containment is scoped per-artifact, not global.**
   `coldstart.py::_is_prerequisite_created` resolves both the cited path
   and that specific artifact's own evidence root before comparing, and
   restricts the check to that root — not any path containing the
   substring `/evidence/` anywhere on the filesystem, which would readmit
   paths belonging to an entirely different project's tree.

**Status:** all six rules are **IMPLEMENTED** in `acceptance.py`,
`coldstart.py`, and `pathdecode.py` (rule 5, decoder-level traversal
rejection, with the calling connectors' error handling covered by
`tests/integration/test_connector_hostile_dirs.py`). This section
documents the pattern as a *policy* so it is applied consistently to any
future code that resolves an untrusted path.

## What requires explicit human approval

| Action | Approval required |
|---|---|
| Enabling `Write`, `Edit`, `NotebookEdit`, or unscoped `Bash` for any agent call | Yes — architectural change requiring an ADR superseding ADR-004, not a config toggle |
| Adding a `--apply` flag or any TUI action that writes to a managed repository | Yes — explicitly forbidden by ADR-004; would require a new ADR and a corresponding rewrite of `SAFE-001`'s guarantee in `THREAT-MODEL.md` |
| Defining any `AgentDefinition` with `permissionMode` or `mcpServers` set | Yes — see `THREAT-MODEL.md` §5. This must go through the same review that would authorize a subagent conformance test, since it changes the security model, not just a config value |
| Registering an MCP server or custom tool | Yes — each tool must be typed, bounded, and reviewed individually against `SDK-CAPABILITY-MATRIX.md` disposition #10 before being wired into any agent call |
| Loading a plugin (`plugins=` on `ClaudeAgentOptions`) | **No approval path exists — this is prohibited outright**, not gated behind approval. See `THREAT-MODEL.md` §11. |
| Bumping the `claude-agent-sdk` pin | Yes, and specifically requires re-running `tools/sdk-inventory.py`, regenerating `SDK-VERSION-LOCK.md`, and producing fresh conformance evidence in the same pull request — an SDK bump without that evidence is defined as a G3 failure in `GATE-MATRIX.md` |
| Widening `spend_cap_usd` beyond an operator's own explicit config change | No code-level approval gate exists; this is an operator-controlled config value (`config.py::Config.spend_cap_usd`) and intentionally so — the cap defaults to `None`, which disables paid agent analysis entirely until an operator sets a positive value |

## Testing this policy

`tests/sdk_conformance/` is not empty — it holds `test_sdk_availability.py`
(collection sentinel), `test_installed_contract.py` (schema/constructor
checks against the installed SDK build — confirms fields, literals, and
dataclass shapes exist; asserts nothing about runtime permission
enforcement), and `test_mutation_denial.py` (the one file that attempts
runtime safety evidence, and does not yet provide it — see "Current
implementation status" above). None of the three closes any item below.
When items 1-6 below are written or fixed, they must exercise, per
`GATE-MATRIX.md` G3/G6:

1. Two separate sub-checks, because `tools=[]` and bare-name
   `disallowed_tools` both make an actual `Write` *attempt* undetectable
   by design (§ item 3 caveat below applies equally here) — the tool
   is simply not present for the model to try:
   - **Availability check:** with the target configuration in place,
     inspect whatever the SDK exposes as the model's offered/available
     tool set at session start (not an attempt-then-observe pattern) and
     assert `Write`/`Edit`/`NotebookEdit`/`Bash` are absent from it — this
     is the check the SDK's own `tools=[]` docstring predicts should
     always pass, so it is the primary evidence for this row.
   - **Enforcement check:** register a visible custom "canary" tool via
     the `PreToolUse` hook path (not a built-in that may be hidden from
     context) and confirm the hook's deny decision is observable when the
     model is prompted to invoke it — proving the hook mechanism itself
     fires and denies, independent of whether `tools`/`disallowed_tools`
     already hide the built-ins.
   - **Filesystem check, independent of both above:** the scratch-path
     non-mutation assertion `test_mutation_denial.py` already performs
     (no file lands) stays as an end-to-end backstop regardless of which
     of the two mechanisms above is doing the work.
1b. Once `Read`/`Grep`/`Glob` are ever added: the `PreToolUse` path-scoping
   hook rejects reads targeting each of — a managed target repository path,
   a harness store path (e.g. `~/.claude/projects`), `~/.9router/auth/cli-secret`,
   `~/.ssh`, and `/etc` — as five separate cases, plus a case where a
   symlink planted *under* the one allowed evidence root resolves to one of
   the denied paths above (the "allowed-root symlink escape" case) and is
   still rejected by the walk-before-resolve check.
2. A `PreToolUse` hook denial fires and is observable in the returned
   `AgentResult`/`ResultMessage`, independent of `allowed_tools`.
3. A subagent defined with `permissionMode="bypassPermissions"` under a
   parent running `dontAsk` is confirmed to either escalate (proving the
   threat in `THREAT-MODEL.md` §5 is real and must be guarded against
   structurally) or be blocked (proving some SDK-level containment exists
   that this document has not yet found documentation for). Either outcome
   must be recorded with evidence, not assumed.
4. A manifest with a symlinked artifact path is rejected by
   `acceptance.py`, and a manifest with a symlinked run root is rejected,
   as two separate test cases — the mechanisms are different code paths.
5. A bare `disallowed_tools=["Bash"]` entry is confirmed to remove `Bash`
   from the model's available tool set, contrasted against a scoped
   `disallowed_tools=["Bash(rm *)"]` entry confirmed to leave `Bash`
   otherwise available — making the distinction in this document
   empirically verified rather than asserted from documentation alone.
6. **Project-path-decoder traversal.** `tests/unit/test_pathdecode_traversal.py`
   covers this directly:
   - `decode_claude_project_dir` raises `ValueError` for a bare `..` tail
     with no matched prefix (`-Users-..`, `-var-..`, `-tmp-..`)
     (`test_traversal_tail_raises_rather_than_truncating`).
   - Whether a `..` tail after a *matched* prefix raises is
     layout-dependent — it raises only when the matched directory
     actually exists on disk; when it doesn't, the whole string is
     treated as one literal filename with no traversal
     (`test_matched_prefix_leaves_bare_traversal_and_raises` vs.
     `test_without_the_matching_child_it_is_one_literal_name`, and
     `test_deep_match_then_traversal_raises` for the same case after
     several matched components). Both branches are pinned explicitly so
     a refactor cannot silently change which one runs.
   - `prefer_cwd()` rejects a hostile native `cwd` field and falls back to
     the safe decoded value, or to empty when neither side is safe
     (`test_prefer_cwd_rejects_hostile_native_field`,
     `test_prefer_cwd_returns_empty_when_neither_is_safe`).
   - The connector-level integration test,
     `tests/integration/test_connector_hostile_dirs.py`, asserts a
     crafted directory is skipped and does not abort the rest of that
     connector's scan — covering all three connectors' `try/except
     ValueError` handling around the decode call.

`THREAT-MODEL.md`'s summary table is the authoritative record of which
parts of this policy are implemented today versus specified for later.
