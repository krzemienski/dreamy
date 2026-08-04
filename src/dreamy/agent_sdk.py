"""Agent SDK wrapper — routes through claude_agent_sdk -> Nine Router.

Three contracts this wrapper enforces before returning:
  1. Real JSON-schema validation of `ResultMessage.structured_output`
     against the caller-supplied `schema` argument. A schema mismatch is
     a hard error — no fallback, no synthesized dict.
  2. Per-run positive spend cap. `_sum_run_cost(store, run_id)` is the
     ledger total for this run. Before any PAID SDK call, `remaining`
     must be > 0; failure returns `stop_reason='disabled'` (non-raising)
     so the per-project loop in `run.py` can mark the project
     `agent_skipped` with reason "agent_disabled_no_cap".
  3. Post-call ledger audit. After the PAID call, the cost is flushed to
     the ledger first; THEN the ledger is re-read to verify the run
     total <= cap. A post-call overshoot (rare; caused by cost reporting
     latency or by the call returning a body but failing the schema) is
     marked `cap_exceeded_post`, the validated `structured_output`
     (if any) is RETAINED on the AgentResult and in the ledger row, never
     discarded — completed paid work is recorded, not erased.

     Retained for AUDIT, not for use: `error_text` is also set, and every
     caller gates on `if result.error_text or not result.structured_output`,
     so an overshoot is a no-op downstream. The two are consistent — the
     evidence survives, the findings are not persisted.

Auth: read NINEROUTER_API_KEY at call time; never persist.
The compiler agent does NOT route through here (it is deterministic).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from dreamy.sdk_policy import AGENT_SDK_POLICY_NAME, apply_policy


@dataclass
class AgentResult:
    content: str = ""
    structured_output: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    model: str = ""
    stop_reason: str = ""
    # Non-empty iff the call did NOT return a schema-valid structured
    # output. Callers check this first; if set, structured_output is
    # always {} and the cost_usd reflects what the SDK reported (which
    # may be 0.0 if the SDK subprocess aborted on a per-call ceiling).
    error_text: str | None = None

class SpendCapExceeded(RuntimeError):
    """Legacy compatibility. `call_claude` no longer raises this — the
    pre-call and post-call guards now return `AgentResult(stop_reason=
    'disabled', error_text=...)` so the caller can decide whether to
    mark `agent_skipped`. Some callers still import this class as a
    type for `except` blocks; keeping it defined avoids surprises."""
    pass

# `_is_unlimited(cfg)` was removed 2026-07-31. It had zero call sites, and its
# name inverted its meaning: it returned True when the cap was unset/<=0, which
# disables paid agents rather than making spend unlimited. The live check is
# `call_claude`'s own pre-call guard, which reads the cap directly.


def _sum_run_cost(store, run_id: str | None) -> float:
    if store is None or run_id is None:
        return 0.0
    row = store.conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM agent_calls WHERE run_id=?",
        (run_id,),
    ).fetchone()
    return float(row["total"] or 0.0)



def _ledger_error(model: str, exc: Exception, *, phase: str) -> AgentResult:
    """A store failure, reported as itself.

    `call_claude` promises never to raise on model failure, but a closed or
    locked SQLite handle is not a model failure and was escaping anyway. It is
    reported with `stop_reason="error"` and a message naming the ledger, so an
    operator is not sent looking at the model or the router.
    """
    return AgentResult(
        model=model,
        stop_reason="error",
        error_text=f"ledger unavailable while {phase}: {type(exc).__name__}: {exc}",
    )

def _max_call_cost_usd(store, run_id: str | None) -> float | None:
    """Highest per-call `cost_usd` recorded for this run. Returns
    `None` if no paid call has completed yet (so the cost-aware skip
    gate lets the FIRST paid call through). The post-call ledger audit
    is the single source of truth — same row that `_sum_run_cost`
    reads. This helper exists so the pre-call gate can reason about
    "would the next call obviously exceed cap" without re-querying
    the whole ledger."""
    if store is None or run_id is None:
        return None
    row = store.conn.execute(
        "SELECT MAX(cost_usd) AS m FROM agent_calls "
        "WHERE run_id=? AND cost_usd > 0.0",
        (run_id,),
    ).fetchone()
    if row is None or row["m"] is None:
        return None
    return float(row["m"])


def _router_api_key() -> str:
    # Read the Nine Router local API key. Env var wins (configured schedule path);
    # fall back to the on-disk cli-secret that VG-0 proved is the working route on
    env_val = os.environ.get("NINEROUTER_API_KEY", "")
    if env_val:
        return env_val
    try:
        secret_path = Path.home() / ".9router" / "auth" / "cli-secret"
        if secret_path.exists():
            return secret_path.read_text().strip()
    except OSError:
        pass
    return ""



def resolve_cli_path(cfg=None) -> str | None:
    """Deterministic claude CLI resolution for launchd (minimal PATH).
    Order: cfg.claude_cli_path → ~/.local/bin/claude → shutil.which. Validates executable."""
    candidates = []
    explicit = getattr(cfg, "claude_cli_path", None) if cfg else None
    if explicit:
        candidates.append(explicit)
    candidates.append(str(Path.home() / ".local" / "bin" / "claude"))
    for c in candidates:
        if c and os.access(c, os.X_OK) and os.path.isfile(c):
            return c
    found = shutil.which("claude")
    return found or None


def call_claude(
    prompt: str,
    *,
    schema: dict | None = None,
    system: str | None = None,
    model: str = "cc/claude-opus-5",
    max_budget_usd: float | None = None,
    env: dict | None = None,
    cwd: str | None = None,
    run_id: str | None = None,
    agent_type: str = "research",
    store=None,
    cfg=None,
) -> AgentResult:
    """Call the model. Returns AgentResult.

    Never raises — not merely on model failure. Every caller treats this as a
    total function and marks a project skipped on a non-`ok` stop_reason, so an
    escaping exception aborts a whole run instead of degrading one project.

    Two distinct non-`ok` outcomes are deliberately NOT merged:

    * `stop_reason="disabled"` — the call was refused before spending, because
      the cap is unset, exhausted, or unmeasurable.
    * `stop_reason="error"` — something failed. A ledger failure says so in
      `error_text` rather than presenting as a model failure, which would send
      an operator to the wrong subsystem.
    """

    # Cap admissibility is decided FIRST — before the router secret is read
    # from disk, before any env is assembled, before CLI resolution. None of
    # that work is justified for a call this function is going to refuse, and
    # a missing key would otherwise return `unavailable` and mask the real
    # reason (no ledger) behind an infrastructure-looking one.
    cap = getattr(cfg, "spend_cap_usd", None) if cfg is not None else None
    if cap is None or cap <= 0:
        # Null disables paid agent analysis.
        return AgentResult(
            model=model,
            stop_reason="disabled",
            error_text="agent analysis disabled: configure a positive spend_cap_usd",
        )

    # A positive cap is only meaningful with a ledger to measure against.
    # `_sum_run_cost` returns 0.0 when either is missing, so the cap silently
    # never binds: every call sees a full budget remaining. Both parameters
    # default to None on a public function, so this is reachable by omission
    # rather than by malice — a probe spent $1.80 against a $1.00 cap.
    #
    # `max_budget_usd` on the SDK options is NOT a substitute: it bounds a
    # single subprocess, not the aggregate across calls, which is the whole
    # point of a per-run cap.
    if store is None or not run_id:
        return AgentResult(
            model=model,
            stop_reason="disabled",
            error_text=(
                "agent analysis disabled: a per-run spend cap requires both a "
                "store and a run_id to measure spend against; refusing to make "
                "a paid call whose cost cannot be accounted for"
            ),
        )

    # The ledger is READ here too, not just checked for existence. A closed or
    # locked store makes the cap unmeasurable, and that must be reported as
    # such — if this were left until after the auth check, a machine with both
    # problems would report `unavailable` and the accounting failure would
    # never surface.
    try:
        spent = _sum_run_cost(store, run_id)
        max_call_usd = _max_call_cost_usd(store, run_id)
    except Exception as exc:  # noqa: BLE001 — any store failure, before spending
        return _ledger_error(model, exc, phase="reading the spend ledger")
    remaining = cap - spent

    if remaining <= 0:
        # Edge case: spent == cap exactly. Skip with disabled.
        return AgentResult(
            model=model,
            stop_reason="disabled",
            error_text=f"agent already spent ${spent:.4f} of ${cap:.4f}; no remaining",
        )
    if max_call_usd is not None and remaining < max_call_usd:
        # We can prove the next call WOULD exceed cap — skip cleanly
        # without raising and without spending anything new.
        return AgentResult(
            model=model,
            stop_reason="disabled",
            error_text=(
                f"agent_skipped_insufficient_remaining: ${remaining:.4f} "
                f"remaining vs max-observed per-call ${max_call_usd:.4f}; "
                f"cap ${cap:.4f}"
            ),
        )

    started_ms = int(time.time() * 1000)
    base_env = {
        "ANTHROPIC_BASE_URL": (cfg.ninerouter_base_url if cfg else "http://localhost:20128"),
        "ANTHROPIC_API_KEY": _router_api_key(),
    }
    if env:
        # Non-auth vars only; routing identity (URL+key) is authoritative and
        # bearer auth is always rejected, so a caller cannot shadow the proven path.
        for _k, _v in env.items():
            if _k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
                continue
            base_env[_k] = _v
    base_env.pop("ANTHROPIC_AUTH_TOKEN", None)
    if not base_env.get("ANTHROPIC_API_KEY"):
        # The ledger row records that a call was attempted and cost nothing.
        # It is bookkeeping, not the verdict: if the store is unusable the
        # answer is still "authentication unavailable", so a failure here is
        # swallowed rather than allowed to escape and mask the real cause.
        # Nothing was spent, so nothing is lost by not recording it.
        if store is not None:
            try:
                unavailable_call_id = store.insert_agent_call(
                    run_id=run_id,
                    agent_type=agent_type,
                    started_ms=started_ms,
                    model=model,
                    input_fingerprint=_sha256(prompt),
                )
                store.finish_agent_call(
                    unavailable_call_id,
                    ended_ms=int(time.time() * 1000),
                    status="unavailable",
                    cost_usd=0.0,
                    tokens=(0, 0),
                    output_json="{}",
                    error_text="Nine Router authentication unavailable",
                )
            except Exception:  # noqa: BLE001 — bookkeeping only; nothing spent
                pass
        return AgentResult(
            content="",
            structured_output={},
            cost_usd=0.0,
            model=model,
            stop_reason="unavailable",
            error_text="Nine Router authentication unavailable",
        )

    # === Spend-cap pre-call audit (contract 2) =========================
    # Ledger is the run's authoritative post-call total; the SDK's
    # `max_budget_usd` is the per-subprocess hard ceiling. We use BOTH:
    # the cost-aware pre-call gate (this section) skips when we can
    # *prove* a subsequent call would push us past `cap`, and the
    # SDK budget adds belt-and-suspenders so a single unexpectedly
    # costly call cannot exceed `cap` either. The earlier "10 errors"
    # class of failure happened because the cap (sized at 85% of a
    # pong probe) was smaller than even one real call's cost, so the
    # SDK subprocess aborted at exit-1 for every attempt. That is a
    # CAP-SIZING failure, not a wrapper failure, and is fixed by
    # calibrating `cap` from a real probe and using the cost-aware
    # gate to skip projects, not by deleting the safety net.

    # Hard ceiling on this single subprocess. The caller's
    # `max_budget_usd` (if any) is honored but always clamped to
    # `remaining` so a single paid call cannot push the run over
    # `cap`. With a calibrated cap and the cost-aware gate above,
    # this ceiling is only relevant as the last defense against
    # an unexpectedly expensive call.
    if max_budget_usd is None:
        call_budget = remaining
    else:
        call_budget = min(max_budget_usd, remaining)
    # Resolve the claude CLI deterministically (launchd has a minimal PATH).
    _cli_path = resolve_cli_path(cfg)
    if not _cli_path:
        return AgentResult(
            model=model,
            stop_reason="error",
            error_text="claude CLI not found; set cfg.claude_cli_path or install ~/.local/bin/claude",
        )
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        model=model,
        env=base_env,
        cwd=cwd,
        # Per-subprocess hard ceiling. With a calibrated cap and
        # the cost-aware pre-call gate above, this is the last
        # defense against an unexpectedly expensive single call.
        max_budget_usd=call_budget,
        cli_path=_cli_path,
    )
    try:
        # No probe/test hook here on purpose. An env var that can inject an
        # unsupported field into the policy is a second code path through the
        # one gate that stands between this wrapper and an unrestricted model
        # call — and the production path would be the untested one. Callers
        # verifying fail-closed behavior call `apply_policy` directly.
        apply_policy(options)
    except Exception as exc:  # noqa: BLE001 — policy failure must stop before SDK invocation
        return AgentResult(
            model=model,
            stop_reason="error",
            error_text=f"SDK capability policy {AGENT_SDK_POLICY_NAME!r} unavailable: {type(exc).__name__}: {exc}",
        )
    if schema is not None:
        options.output_format = {"type": "json_schema", "schema": schema}
    if system is not None:
        options.system_prompt = system


    result = AgentResult(model=model)
    call_id: str | None = None
    # Preflight ledger work is guarded separately from the SDK call below. A
    # broad try around both would report a closed or locked database as a model
    # failure, which is the wrong diagnosis and the wrong remedy. Nothing has
    # been spent yet at this point, so returning here costs nothing.
    try:
        call_id = store.insert_agent_call(
            run_id=run_id,
            agent_type=agent_type,
            started_ms=started_ms,
            model=model,
            input_fingerprint=_sha256(prompt),
        )
    except Exception as exc:  # noqa: BLE001 — any store failure, before spending
        return _ledger_error(model, exc, phase="opening the ledger row")

    # === Single paid attempt ===========================================
    try:
        text, structured_obj, stop_reason, cost_usd, usage = _run_query_sync(prompt, options)
    except Exception as exc:  # noqa: BLE001
        # Persist the failure row so the ledger reflects zero cost and
        # the operator can see WHY the call failed.
        if store is not None and call_id is not None:
            store.finish_agent_call(
                call_id,
                ended_ms=int(time.time() * 1000),
                status="error",
                cost_usd=0.0,
                tokens=(0, 0),
                output_json="{}",
                error_text=f"{type(exc).__name__}: {exc}",
            )
        result.error_text = f"{type(exc).__name__}: {exc}"
        result.stop_reason = "error"
        return result

    result.content = text or ""
    if stop_reason:
        result.stop_reason = stop_reason
    result.cost_usd = float(cost_usd or 0.0)
    if usage:
        result.usage = dict(usage)

    # === Schema validation (contract 1) ================================
    if schema is not None:
        verr = _validate_structured(structured_obj, schema)
        if verr is not None:
            # Schema mismatch — RETAIN the cost but DROP the structured
            # output. Caller must not persist anything from this call.
            if store is not None and call_id is not None:
                store.finish_agent_call(
                    call_id,
                    ended_ms=int(time.time() * 1000),
                    status="schema_invalid",
                    cost_usd=result.cost_usd,
                    tokens=(
                        int(usage.get("prompt_tokens") or 0) if usage else None,
                        int(usage.get("completion_tokens") or 0) if usage else None,
                    ),
                    output_json="{}",
                    error_text=f"schema_invalid: {verr}",
                )
            result.error_text = f"schema_invalid: {verr}"
            result.stop_reason = "schema_invalid"
            # No silent retry — the model could return the same shape.
            return result
        if isinstance(structured_obj, dict):
            result.structured_output = structured_obj
    elif isinstance(structured_obj, dict):
        # No schema declared but the model still returned structured output.
        # Caller didn't validate it; we pass the dict through unchanged
        # so behavior matches what the caller explicitly asked for.
        result.structured_output = structured_obj

    # === Persist `ok` row, then run post-call ledger audit (contract 3)
    if store is not None and call_id is not None:
        store.finish_agent_call(
            call_id,
            ended_ms=int(time.time() * 1000),
            status="ok",
            cost_usd=result.cost_usd,
            tokens=(
                int(usage.get("prompt_tokens") or 0) if usage else None,
                int(usage.get("completion_tokens") or 0) if usage else None,
            ),
            output_json=json.dumps(result.structured_output or {}),
            error_text=None,
        )
        # Post-call ledger re-read. If the actual cost pushed the run
        # total past the cap (cost reporting latency, unusual rounding),
        # mark the just-persisted row honestly as `cap_exceeded_post`,
        # and RETAIN result.structured_output (spec: never discard
        # completed paid work, only mark).
        post_total = _sum_run_cost(store, run_id)
        if post_total > cap + 1e-9:
            overshoot = post_total - cap
            # Through `finish_agent_call`, not a raw UPDATE. The interpolated
            # value is numeric today, so this was structural rather than a live
            # leak — but a raw write here is exactly how the boundary erodes:
            # the next person to add a field to this message would have no
            # redaction on it, and nothing would flag that.
            try:
                store.finish_agent_call(
                    call_id,
                    ended_ms=int(time.time() * 1000),
                    status="cap_exceeded_post",
                    # Same values the `ok` row above wrote: this re-finish
                    # marks the row, it does not re-measure it. Reading a
                    # different usage key here would silently blank the token
                    # counts, and `_sum_run_cost` reads `cost_usd` directly.
                    cost_usd=result.cost_usd,
                    tokens=(
                        int(usage.get("prompt_tokens") or 0) if usage else None,
                        int(usage.get("completion_tokens") or 0) if usage else None,
                    ),
                    output_json=json.dumps(result.structured_output or {}),
                    error_text=f"post-call ledger exceeded cap by ${overshoot:.4f}",
                )
                store.commit()
                marker_error = ""
            except Exception as exc:  # noqa: BLE001 — verdict stands regardless
                # The verdict below is computed from the ledger read, so it is
                # correct whether or not this marker persisted. But the caller
                # otherwise cannot tell that the row still says `ok` — the
                # durable record and the returned verdict would disagree with
                # nothing saying so.
                marker_error = (
                    f"; marker persistence could not be confirmed "
                    f"({type(exc).__name__}: {exc}) — the durable row may still "
                    "read `ok`"
                )
            result.error_text = (
                f"cap_exceeded_post: total ${post_total:.4f} exceeds cap "
                f"${cap:.4f}{marker_error}"
            )
    return result



def _validate_structured(obj, schema: dict) -> str | None:
    """Validate the model-returned `structured_output` against the declared
    JSON schema. Returns None on success, an error string on failure.
    Uses `jsonschema` if importable; otherwise a minimal required-keys +
    primitive-type walker. NEVER fabricates a dict to make validation pass."""
    if not isinstance(obj, dict):
        return f"structured_output is {type(obj).__name__}, expected dict"
    try:
        import jsonschema  # noqa: WPS433 — local import keeps module-load cold
        from jsonschema import Draft202012Validator
    except ImportError:
        jsonschema = None  # type: ignore[assignment]

    if jsonschema is not None:
        try:
            Draft202012Validator(schema).validate(obj)
            return None
        except Exception as exc:  # noqa: BLE001 — return validator message
            msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            return f"{type(exc).__name__}: {msg[:200]}"

    # Stdlib fallback. This is the DEFAULT production path, not a rare
    # degradation: `dependencies = []` in pyproject.toml, so `jsonschema` is
    # present only transitively (via `mcp`) in some environments and absent in
    # a plain install. It therefore has to actually validate.
    #
    # A previous version walked one level and claimed to recurse. It accepted
    # a nested type violation, a bad array item, and a boolean where an integer
    # was required — the last because `isinstance(True, int)` is True, so the
    # explicit boolean guard below the type check could never be reached.
    #
    # Scope is deliberate: the keywords the agent schemas actually use. An
    # unrecognised keyword is ignored rather than treated as a failure, so this
    # never rejects output that real `jsonschema` would accept.
    return _walk_schema(obj, schema, path="$")


_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def _type_error(value, tname: str, path: str) -> str | None:
    """Return an error when *value* does not match JSON type *tname*."""
    py_types = _TYPE_MAP.get(tname)
    if py_types is None:
        return None  # unknown type keyword — do not invent a rule
    # `bool` is a subclass of `int`, so it must be excluded BEFORE the
    # isinstance check rather than after it.
    if tname in ("integer", "number") and isinstance(value, bool):
        return f"{path}: expected {tname}, got boolean"
    if not isinstance(value, py_types):
        return f"{path}: expected {tname}, got {type(value).__name__}"
    return None


def _walk_schema(value, schema, *, path: str) -> str | None:
    """Recursively validate *value* against *schema*. None means valid."""
    if not isinstance(schema, dict):
        return None

    declared = schema.get("type")
    if isinstance(declared, str):
        err = _type_error(value, declared, path)
        if err:
            return err
    elif isinstance(declared, list) and declared:
        if all(_type_error(value, t, path) for t in declared if isinstance(t, str)):
            return f"{path}: expected one of {declared}, got {type(value).__name__}"

    if "enum" in schema and isinstance(schema["enum"], list):
        if value not in schema["enum"]:
            return f"{path}: {value!r} is not one of {schema['enum']}"

    if isinstance(value, dict):
        for key in schema.get("required", []) or []:
            if key not in value:
                return f"{path}: missing required key: {key!r}"
        properties = schema.get("properties", {}) or {}
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                return f"{path}: undeclared keys {extra}"
        for key, sub in properties.items():
            if key in value:
                err = _walk_schema(value[key], sub, path=f"{path}.{key}")
                if err:
                    return err

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, element in enumerate(value):
                err = _walk_schema(element, items, path=f"{path}[{index}]")
                if err:
                    return err

    return None


async def _collect_query(prompt: str, options):
    """Consume the async query() generator. Returns (text, structured, stop_reason, cost, usage)."""
    from claude_agent_sdk import ResultMessage, query

    text_parts: list[str] = []
    structured_obj = None
    stop_reason = ""
    cost_usd = 0.0
    usage: dict = {}
    async for msg in query(prompt=prompt, options=options):
        if not isinstance(msg, ResultMessage):
            continue
        sr = getattr(msg, "stop_reason", None)
        if sr:
            stop_reason = sr
        if getattr(msg, "structured_output", None) is not None:
            structured_obj = msg.structured_output
        if getattr(msg, "result", None):
            text_parts.append(str(msg.result))
        tc = getattr(msg, "total_cost_usd", None)
        if tc is not None:
            cost_usd = float(tc or 0.0)
        u = getattr(msg, "usage", None)
        if u:
            usage = dict(u)
    return "".join(text_parts), structured_obj, stop_reason, cost_usd, usage


def _run_query_sync(prompt: str, options):
    """Sync bridge over the async query(). Refuses to nest inside a running loop
    (e.g. Textual's event loop) — call_claude is a pipeline-time (sync) API only."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_collect_query(prompt, options))
    raise RuntimeError(
        "call_claude cannot run inside an active event loop; invoke from pipeline context"
    )


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
