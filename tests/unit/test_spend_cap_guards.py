"""The spend cap must be decided before any secret is read or money spent.

Three defects, all found by review rather than by these tests existing:

* H-6: `store` and `run_id` both default to `None` on a public function, and
  `_sum_run_cost` returns 0.0 when either is missing. The cap silently never
  bound — a probe spent $1.80 against a $1.00 cap. `max_budget_usd` is not a
  substitute: it bounds one subprocess, not the aggregate.
* H-7: the ledger row id was derived from
  `(run_id, agent_type, started_ms, input_fingerprint)`, so two identical
  prompts in the same millisecond collided and raised `IntegrityError` out of
  a function documented not to raise for that class.
* Ordering: the auth check ran before the cap was evaluated, so a machine with
  both an unreadable ledger and a missing key reported `unavailable` and the
  accounting failure never surfaced.

Ordering is the subtle one and is asserted directly: `_router_api_key` is
replaced with a counter, and a refused call must never read the secret.
"""

from __future__ import annotations

import sys
import types

import pytest

from dreamy import agent_sdk
from dreamy.store import Store


class _Cfg:
    spend_cap_usd = 1.0
    ninerouter_base_url = "http://localhost:20128"


@pytest.fixture
def no_paid_calls(monkeypatch):
    """Any real SDK call is a test failure."""

    def _explode(*args, **kwargs):
        raise AssertionError("a paid call was attempted")

    monkeypatch.setattr(agent_sdk, "_run_query_sync", _explode)


@pytest.fixture
def secret_reads(monkeypatch):
    """Counts reads of the router secret, to pin decision ORDER."""
    counter = {"n": 0}

    def _key(value: str = "test-key"):
        counter["n"] += 1
        return value

    monkeypatch.setattr(agent_sdk, "_router_api_key", _key)
    return counter


def _install_sdk_stub(monkeypatch) -> None:
    """Provide the one SDK symbol used before the query.

    `ClaudeAgentOptions` is a plain attribute bag at the point of use — the
    wrapper assigns `output_format` and `system_prompt` after construction — so
    a permissive stand-in exercises the real code path.
    """

    class _CapturingOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        types.SimpleNamespace(ClaudeAgentOptions=_CapturingOptions),
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "ledger.db")
    s.start_run("run-1", 0)
    yield s
    try:
        s.close()
    except Exception:
        pass


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("neither store nor run_id", {}),
        ("store without run_id", {"run_id": None}),
        ("empty run_id", {"run_id": ""}),
    ],
)
def test_missing_ledger_refuses_before_spending(
    label, kwargs, store, no_paid_calls, secret_reads
) -> None:
    """Without a ledger the cap is unmeasurable, so the call is refused."""
    passed = dict(kwargs)
    if "run_id" in passed:
        passed["store"] = store

    result = agent_sdk.call_claude("prompt", cfg=_Cfg(), **passed)

    assert result.stop_reason == "disabled", label
    assert "cannot be accounted for" in (result.error_text or "")
    assert secret_reads["n"] == 0, "refused before the router secret is read"


def test_unreadable_ledger_reports_itself_not_the_model(
    tmp_path, no_paid_calls, secret_reads
) -> None:
    """A closed store is an accounting failure, not a model failure.

    It must not surface as `unavailable` either: that is the auth outcome, and
    a machine with both problems would hide the ledger fault behind it.
    """
    closed = Store(tmp_path / "closed.db")
    closed.start_run("run-1", 0)
    # The closed store IS the fixture — the run row is written only to give
    # `call_claude` something to look up, and is never meant to survive.
    closed.close(discard=True)

    result = agent_sdk.call_claude(
        "prompt", cfg=_Cfg(), store=closed, run_id="run-1"
    )

    assert result.stop_reason == "error"
    assert "ledger unavailable" in (result.error_text or "")
    assert secret_reads["n"] == 0, "ledger decided before auth"


def test_exhausted_cap_refuses_before_auth(store, no_paid_calls, secret_reads) -> None:
    """spent == cap must refuse, and add no bookkeeping row."""
    call_id = store.insert_agent_call(
        run_id="run-1", agent_type="t", started_ms=0, model="m", input_fingerprint="f"
    )
    store.finish_agent_call(call_id, 1, "ok", cost_usd=1.0)
    store.commit()
    before = store.conn.execute("SELECT count(*) FROM agent_calls").fetchone()[0]

    result = agent_sdk.call_claude("prompt", cfg=_Cfg(), store=store, run_id="run-1")

    assert result.stop_reason == "disabled"
    assert "no remaining" in (result.error_text or ""), (
        f"exhaustion must report as exhausted, not as a prediction: "
        f"{result.error_text!r}"
    )
    after = store.conn.execute("SELECT count(*) FROM agent_calls").fetchone()[0]
    assert after == before, "a refused call must not open a ledger row"
    assert secret_reads["n"] == 0


def test_overspent_cap_reports_exhaustion_not_prediction(
    store, no_paid_calls, secret_reads
) -> None:
    """With spent > cap, `remaining` is negative.

    The predictor branch would also match, so ordering decides which message
    an operator sees. "already spent $X of $Y" is the true statement.
    """
    call_id = store.insert_agent_call(
        run_id="run-1", agent_type="t", started_ms=0, model="m", input_fingerprint="f"
    )
    store.finish_agent_call(call_id, 1, "ok", cost_usd=1.5)
    store.commit()

    result = agent_sdk.call_claude("prompt", cfg=_Cfg(), store=store, run_id="run-1")

    assert result.stop_reason == "disabled"
    assert "no remaining" in (result.error_text or "")


def test_remaining_equal_to_max_observed_still_proceeds(
    store, monkeypatch, secret_reads
) -> None:
    """Boundary: `remaining == max_call_usd` must NOT be refused.

    An equal-cost call lands exactly on the cap, which is spending the budget,
    not exceeding it. Pins `<` against a future `<=`.
    """
    call_id = store.insert_agent_call(
        run_id="run-1", agent_type="t", started_ms=0, model="m", input_fingerprint="f"
    )
    store.finish_agent_call(call_id, 1, "ok", cost_usd=0.5)
    store.commit()
    # spent 0.5, cap 1.0 -> remaining 0.5, max observed 0.5

    seen = {}

    def _capture(prompt, options):
        seen["budget"] = getattr(options, "max_budget_usd", None)
        return ("text", None, "end_turn", 0.5, {})

    monkeypatch.setattr(agent_sdk, "_run_query_sync", _capture)
    # A stub rather than `importorskip`: this is the load-bearing assertion for
    # `<` vs `<=`, and skipping it on a machine without the optional SDK would
    # drop the check exactly where a regression is easiest to make. Only
    # `ClaudeAgentOptions` is touched before the query, and the mock above
    # replaces the query itself — no SDK behaviour is under test here, just the
    # wrapper's budget plumbing.
    _install_sdk_stub(monkeypatch)
    monkeypatch.setattr(agent_sdk, "resolve_cli_path", lambda cfg=None: "/usr/bin/true")

    result = agent_sdk.call_claude("prompt", cfg=_Cfg(), store=store, run_id="run-1")

    assert result.stop_reason != "disabled", (
        f"an exactly-affordable call was refused: {result.error_text!r}"
    )
    assert seen["budget"] == pytest.approx(0.5), (
        f"the subprocess ceiling must be the remaining budget, got {seen.get('budget')!r}"
    )
    rows = store.conn.execute("SELECT count(*) FROM agent_calls").fetchone()[0]
    assert rows == 2, f"expected the prior row plus this call, got {rows}"
    assert agent_sdk._sum_run_cost(store, "run-1") == pytest.approx(1.0), (
        "an exactly-affordable call lands ON the cap; the ledger must show it"
    )


def test_identical_calls_get_distinct_ledger_rows(store) -> None:
    """H-7: same prompt, same millisecond.

    Deduplicating would undercount spend, and the cap reads exactly that sum —
    so the rows must be distinct AND the costs must add up.
    """
    ids = []
    for _ in range(3):
        call_id = store.insert_agent_call(
            run_id="run-1",
            agent_type="t",
            started_ms=1000,
            model="m",
            input_fingerprint="identical",
        )
        store.finish_agent_call(call_id, 1001, "ok", cost_usd=0.60)
        ids.append(call_id)
    store.commit()

    assert len(set(ids)) == 3, "ids collided"
    assert store.conn.execute("SELECT count(*) FROM agent_calls").fetchone()[0] == 3
    assert agent_sdk._sum_run_cost(store, "run-1") == pytest.approx(1.80), (
        "each call was really billed; folding them into one row would "
        "undercount spend and the cap reads exactly this sum"
    )
    fingerprints = store.conn.execute(
        "SELECT count(DISTINCT input_fingerprint) FROM agent_calls"
    ).fetchone()[0]
    assert fingerprints == 1, "identical prompts must remain groupable"


def test_post_call_overshoot_marks_the_row_without_re_measuring_it(
    store, monkeypatch, secret_reads
) -> None:
    """A call that lands over cap is marked, not deleted or re-billed.

    The marking write used to be raw SQL, bypassing the redaction boundary
    every other writer goes through. Values interpolated there are numeric
    today, so it was structural rather than a live leak — but it is exactly
    how a boundary erodes, and re-finishing the row is easy to get wrong:
    reading a different usage key silently blanks the token counts, and
    `_sum_run_cost` reads `cost_usd` directly, so a dropped cost would
    under-report the very overshoot being recorded.

    Asserted on the persisted row rather than on which method wrote it.
    """
    _install_sdk_stub(monkeypatch)
    monkeypatch.setattr(agent_sdk, "resolve_cli_path", lambda cfg=None: "/usr/bin/true")

    # Cost exceeds the $1.00 cap, so the post-call audit fires.
    schema = {
        "type": "object",
        "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
        "required": ["findings"],
    }

    def _overshoot(prompt, options):
        return (
            "text",
            {"findings": ["kept for audit"]},
            "end_turn",
            1.5,
            {"prompt_tokens": 10, "completion_tokens": 20},
        )

    monkeypatch.setattr(agent_sdk, "_run_query_sync", _overshoot)

    result = agent_sdk.call_claude(
        "prompt", cfg=_Cfg(), store=store, run_id="run-1", schema=schema
    )

    assert "cap_exceeded_post" in (result.error_text or ""), result.error_text
    assert "NOT marked" not in (result.error_text or ""), (
        f"the marker write failed: {result.error_text}"
    )

    rows = store.conn.execute(
        "SELECT status, cost_usd, prompt_tokens, completion_tokens FROM agent_calls"
    ).fetchall()
    assert len(rows) == 1, f"the overshoot must mark the row, not add one: {rows}"
    status, cost, prompt_tokens, completion_tokens = rows[0]
    assert status == "cap_exceeded_post"
    assert cost == pytest.approx(1.5), "the real cost must survive the marking"
    assert (prompt_tokens, completion_tokens) == (10, 20), (
        f"token counts were blanked by the re-finish: "
        f"({prompt_tokens}, {completion_tokens})"
    )
    assert agent_sdk._sum_run_cost(store, "run-1") == pytest.approx(1.5), (
        "the ledger must still report the overshoot it just recorded"
    )

    # Completed paid work is recorded, not erased — on the result AND in the
    # row. Retained for audit only: `error_text` is set, and every caller
    # gates on it, so nothing downstream persists these findings.
    assert result.structured_output == {"findings": ["kept for audit"]}
    persisted = store.conn.execute("SELECT output_json FROM agent_calls").fetchone()[0]
    assert "kept for audit" in (persisted or ""), (
        f"the validated output was dropped from the ledger row: {persisted!r}"
    )


def test_agent_sdk_never_writes_to_the_database_directly() -> None:
    """Ledger writes must go through `Store`, which redacts.

    Row assertions cannot catch a regression here: the marking path re-finishes
    a row that already holds the right cost and tokens, so a raw
    `store.conn.execute(...)` produces an identical row and every value-based
    test still passes. Mutation-checked — reverting the fix left the suite
    green, which is what makes this structural check necessary.

    Enforced as an ALLOWLIST of enclosing functions rather than by inspecting
    the SQL text. Reading the verb out of a string literal fails open on
    `sql = "UPDATE ..."; conn.execute(sql)`, where the argument is a Name and
    the check sees nothing. The two read helpers below are the only places in
    this module permitted to touch the connection at all; anything else must
    go through a `Store` method.
    """
    import ast
    import inspect

    permitted = {"_sum_run_cost", "_max_call_cost_usd"}
    tree = ast.parse(inspect.getsource(agent_sdk))

    found: dict[str, list[int]] = {}
    for parent in ast.walk(tree):
        if not isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(parent):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "conn"
            ):
                found.setdefault(parent.name, []).append(node.lineno)

    offenders = {name: lines for name, lines in found.items() if name not in permitted}
    assert not offenders, (
        "agent_sdk touches the database connection directly outside its two "
        f"read helpers, bypassing the redaction boundary in Store: {offenders}"
    )
    assert set(found) <= permitted
