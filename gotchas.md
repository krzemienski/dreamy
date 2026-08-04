# Gotchas

## "use the role task model" — explicit fields, not effort tiers
- When dispatching subagents, the user means explicit `role`, `task`, `model` named fields.
- Do NOT use generic `agent: scout` + `effort: med` shape.
- Subagents routed to the default scout/anthropic provider 404 instantly on this machine
  ("No active credentials for provider: anthropic"). The only proven working model path
  is Nine Router at localhost:20128 (cc/claude-opus-5) or the session default model.
- Re-dispatch with `agent: "task"` + an explicit working `model` (e.g. "default"), or inline.

## Secret hygiene — never print token bytes/prefixes
- Verification of a secret must print ONLY boolean + length, never the value or a prefix.
- Printing `token[:4]` exposed material into the session/tool log. Future checks: `bool(tok)`,
  `len(tok)`. Never slice, never repr.
- Ensure no acceptance artifact under ~/.local/share/dreamy/acceptance/ captures secret
  material — grep captured evidence for the token prefix before marking a gate PASS.

## Edit Integrity — imports go module-level
- When a fix adds an import inside a function, promote it to module level in the same edit
  pass. Re-read the edited file before the next edit; inner imports shadow and linger.

## 2026-08-04 — serial execution where parallel was available

**Correction:** "why is this taking so long orchestrate sub agents"

**What I did wrong.** I ran I2 and I5 end-to-end myself — read source, edit,
probe, fix, re-probe — one tool call at a time, across dozens of turns. The
two improvements touched disjoint modules (`protocol.py`/`connectors/*` vs
`bundle.py`/`read.py`/`importer.py`) and could have run as two parallel
subagents from the start. Only the docs work got fanned out.

**Why it happened.** Each advisory I received was correct and specific, so I
kept fixing narrowly and never stepped back to ask whether the REMAINING work
was still divisible. Reacting to good feedback is not the same as planning.

**Rule.** Before starting any multi-item phase, list the items and their file
scopes. If two items have disjoint scope, they dispatch together in ONE
message — not sequentially, not "after this one lands". Serial execution is
justified only by a real data dependency (one item produces a contract the
next consumes), and that dependency must be stated when claimed.

**Applied here.** I3 (store/config/run + `cost`), I4 (read + `findings`),
publish prep (CI/gitignore/pyproject), and the history audit (read-only) went
out as four parallel lanes with explicit file ownership and a shared migration
contract (v8 shipped, I3 takes v9, I4 takes none).
