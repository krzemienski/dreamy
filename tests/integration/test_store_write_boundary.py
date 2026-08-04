"""SAFE-002 across the WHOLE write boundary, not one method at a time.

Three separate leaks were found here by three separate reviewers, each in a
sibling of the last: `finish_agent_call` was fixed, then `insert_agent_event`
and `insert_finding_with_provenance` were found unfixed, then `insert_router`,
then `insert_turn` — the highest-volume path in the system, one row per turn of
every ingested session.

The pattern was the method: each fix addressed the reported site instead of the
boundary. These drive every writer that exists TODAY with a canary in every
free-text argument, and assert the canary appears in no column and nowhere in
the database file.

That alone would not catch a NEW writer — `_write_everything` calls methods by
name, so an unlisted one is simply never exercised. `test_every_writer_is_classified`
closes that by comparing the live set of mutating methods against an explicit
allowlist: adding a writer fails until it is classified here.

Identifier exceptions are asserted positively rather than left implicit, since
"it does not appear anywhere" would also hold if the writes silently failed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from dreamy.store import Store

CANARY = "sk-ant-api03-BOUNDARYCANARY000000000000000000000000000000000000"

# Columns that MUST hold an unredacted value: identifiers that rows are keyed
# and joined by. Redacting them would break correlation for exactly the records
# whose values matched a secret pattern.
IDENTIFIER_COLUMNS = {
    ("projects", "path"),
    ("sessions", "source_id"),
    ("sessions", "native_id"),
    ("router_requests", "source_id"),
    ("router_requests", "native_id"),
    ("turns", "content_fingerprint"),
    ("agent_calls", "input_fingerprint"),
    ("skill_pins", "content_sha256"),
}


def _write_everything(store: Store) -> dict[str, str]:
    """Drive every writer with the canary in every free-text argument."""
    store.start_run("run-1", 0)
    project_id = store.upsert_project(
        "/tmp/proj", f"name {CANARY}", 0, 0, f"https://u:{CANARY}@h/r.git"
    )
    session_id = store.upsert_session(
        project_id,
        "claude",
        "native-1",
        0,
        1,
        f"model {CANARY}",
        f"branch {CANARY}",
        f"/tmp/{CANARY}.jsonl",
        1,
    )
    store.insert_turn(
        session_id,
        "claude",
        0,
        "user",
        f"model {CANARY}",
        "fingerprint",
        f"tool {CANARY}",
        f'["/p/{CANARY}"]',
        f"err {CANARY}",
        f'{{"m":"{CANARY}"}}',
    )
    call_id = store.insert_agent_call(
        run_id="run-1",
        agent_type="teacher",
        started_ms=0,
        model=f"model {CANARY}",
        input_fingerprint="fingerprint",
    )
    store.insert_agent_event(
        "run-1",
        f"agent {CANARY}",
        call_id,
        0,
        f"topic {CANARY}",
        f"level {CANARY}",
        f"message {CANARY}",
        f'{{"k":"{CANARY}"}}',
    )
    store.finish_agent_call(
        call_id,
        1,
        f"ok {CANARY}",
        output_json=f'{{"o":"{CANARY}"}}',
        error_text=f"err {CANARY}",
    )
    store.upsert_git_snapshot(
        "run-1",
        project_id,
        SimpleNamespace(
            status_porcelain=f" M {CANARY}.py",
            recent_log=[{"subject": f"subject {CANARY}", "author": "a"}],
            diff_stat=f"1 file changed, {CANARY}",
            error=f"err {CANARY}",
        ),
        0,
    )
    store.record_run_spend(
        "run-1",
        0,
        1.23,
        # I3: `model` is the only free text this writer binds. Planting the
        # canary here is what makes its REDACTING_WRITERS classification a
        # proof rather than an assertion — if `redact_text` were dropped from
        # `record_run_spend`, the canary would reach the file and
        # `test_database_file_contains_no_secret` would fail.
        f"model {CANARY}",
        2,
    )
    store.insert_router(
        "nine_router",
        "router-1",
        0,
        f"provider {CANARY}",
        f"model {CANARY}",
        f"conn {CANARY}",
        f"status {CANARY}",
        0.0,
        1,
        1,
        f"https://u:{CANARY}@h/v1/messages?api_key={CANARY}",
    )
    store.link_router_request(
        "nine_router", "router-1", session_id, 1, f"high {CANARY}", f"reason {CANARY}"
    )
    finding_id = store.insert_finding_with_provenance(
        project_id,
        {
            "title": f"title {CANARY}",
            "detail": f"detail {CANARY}",
            "evidence": {"quote": CANARY},
            "category": f"category {CANARY}",
            "severity": f"severity {CANARY}",
            "confidence": f"confidence {CANARY}",
            "state": f"state {CANARY}",
        },
        provenance="agent",
    )
    store.dismiss_finding(finding_id, f"reason {CANARY}", 1)
    store.upsert_skill_pin(f"skill {CANARY}", "deadbeef", f"/tmp/{CANARY}/SKILL.md")
    store.finish_run(
        "run-1", 1, f"ok {CANARY}", f'{{"claude": "{CANARY}"}}', 0, 0, f"/tmp/{CANARY}.log"
    )
    store.commit()
    return {"project_id": project_id, "session_id": session_id, "call_id": call_id}


def _all_text_cells(conn: sqlite3.Connection):
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        for row in conn.execute(f"SELECT * FROM {table}"):
            for column, value in zip(columns, row, strict=False):
                yield table, column, value


def test_no_writer_persists_a_secret_in_any_column(tmp_path) -> None:
    store = Store(tmp_path / "boundary.db")
    try:
        _write_everything(store)
        leaks = [
            f"{table}.{column}"
            for table, column, value in _all_text_cells(store.conn)
            if isinstance(value, str) and CANARY in value
        ]
        assert not leaks, f"unredacted columns: {sorted(set(leaks))}"
    finally:
        store.close()


def test_redaction_actually_ran(tmp_path) -> None:
    """Positive control.

    Absence alone would also hold for a store that wrote nothing at all —
    the vacuity mode this codebase keeps rediscovering.
    """
    store = Store(tmp_path / "boundary.db")
    try:
        _write_everything(store)
        redacted = [
            f"{table}.{column}"
            for table, column, value in _all_text_cells(store.conn)
            if isinstance(value, str) and "REDACTED" in value
        ]
        assert len(redacted) >= 20, (
            f"only {len(redacted)} redacted columns — the writes did not land, "
            "so the absence assertions are vacuous"
        )
    finally:
        store.close()


def test_identifier_columns_survive_verbatim(tmp_path) -> None:
    """Identifiers must survive INTACT, not merely un-REDACTED.

    Asserting only that "REDACTED" is absent is weak: a value that was never
    written, or written empty, also satisfies it. Every column in
    IDENTIFIER_COLUMNS is written with the canary as part of the identifier
    and asserted back byte-exact — which is the property correlation depends
    on, and the deliberate disclosure this exception accepts. The set
    comparison below keeps the constant load-bearing: a column added to
    IDENTIFIER_COLUMNS without a matching write here fails the test.
    """
    store = Store(tmp_path / "identifiers.db")
    try:
        store.start_run("run-1", 0)
        project_id = store.upsert_project(f"/tmp/{CANARY}", "n", 0, 0)
        session_id = store.upsert_session(
            project_id, f"src-{CANARY}", f"native-{CANARY}", 0, 1, "m", "b", "/tmp/x", 1
        )
        store.insert_turn(
            session_id, f"src-{CANARY}", 0, "user", "m",
            f"fp-{CANARY}", "t", "[]", "", "{}",
        )
        store.insert_agent_call(
            run_id="run-1", agent_type="teacher", started_ms=0,
            model="m", input_fingerprint=f"ifp-{CANARY}",
        )
        store.insert_router(
            f"src-{CANARY}", f"native-{CANARY}", 0, "p", "m", "c", "ok",
            0.0, 1, 1, "http://h",
        )
        store.upsert_skill_pin("skill", CANARY)
        store.commit()

        expected = {
            ("projects", "path"): f"/tmp/{CANARY}",
            ("sessions", "source_id"): f"src-{CANARY}",
            ("sessions", "native_id"): f"native-{CANARY}",
            ("router_requests", "source_id"): f"src-{CANARY}",
            ("router_requests", "native_id"): f"native-{CANARY}",
            ("turns", "content_fingerprint"): f"fp-{CANARY}",
            ("agent_calls", "input_fingerprint"): f"ifp-{CANARY}",
            ("skill_pins", "content_sha256"): CANARY,
        }
        assert set(expected) == IDENTIFIER_COLUMNS, (
            "IDENTIFIER_COLUMNS drifted from the identifiers this test writes "
            "— update the writes or the constant, never just one"
        )
        for (table, column), want in expected.items():
            got = store.conn.execute(
                f"SELECT {column} FROM {table} WHERE {column} = ?", (want,)
            ).fetchone()
            assert got is not None, (
                f"{table}.{column} was altered: {want!r} not found verbatim. "
                "Rows are keyed and joined by identifiers, so correlation "
                "would break for exactly the records whose values matched a "
                "secret pattern."
            )
    finally:
        store.close()


# Every mutating Store method, classified. A writer absent from BOTH sets fails
# `test_every_writer_is_classified` — which is what makes this a boundary test
# rather than a list of the leaks found so far.
REDACTING_WRITERS = {
    "upsert_project",
    "upsert_session",
    "insert_turn",
    "insert_router",
    "link_router_request",
    "insert_agent_call",
    "finish_agent_call",
    "insert_agent_event",
    "insert_finding_with_provenance",
    "dismiss_finding",
    "upsert_skill_pin",
    "finish_run",
    # R14.c git snapshots. Binds repo-originated free text (status porcelain,
    # recent-log JSON, diff stat, error) — all redacted via redact_text
    # before insert, so it belongs here, not in NON_TEXT_WRITERS.
    "upsert_git_snapshot",
    # I3 spend ledger. `model` is SDK-reported free text and is passed through
    # `redact_text` before insert, so this is a redacting writer, not a
    # numbers-only one — every other bound value is an id or a number.
    "record_run_spend",
}

# Writers that bind NO caller-supplied free text — identifiers and integers only.
NON_TEXT_WRITERS = {
    "set_watermark",
    "start_run",
    # R13 ledgers. Both bind only ids the store itself derived (a finding's
    # stable_id, a run id) plus integers — no title, detail, or evidence text
    # reaches either, so there is nothing for redaction to act on.
    "record_finding_observation",
    "record_analyzed_project",
    # Orphan-run recovery. Binds one integer (the recovery timestamp) and
    # matches on a literal status string the store itself wrote; no caller
    # text of any kind reaches the statement.
    "recover_interrupted_runs",
}


def test_every_writer_is_classified() -> None:
    """A new writer must be classified before it can ship.

    Without this, adding a method that persists model text would leave the
    other tests passing — they call writers by name, so an unlisted one is
    never exercised at all.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(Store))
    mutating = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("_")
        and any(
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            # `INSERT OR IGNORE`/`OR REPLACE` are writes too — matching only
            # "INSERT INTO" silently dropped `insert_turn`, the highest-volume
            # writer in the system, from this contract.
            and (
                "INSERT " in sub.value
                or "UPDATE " in sub.value
                or "DELETE FROM" in sub.value
            )
            for sub in ast.walk(node)
        )
    }
    unclassified = mutating - REDACTING_WRITERS - NON_TEXT_WRITERS
    assert not unclassified, (
        f"unclassified Store writers: {sorted(unclassified)}. Add each to "
        "REDACTING_WRITERS (and exercise it in _write_everything) or to "
        "NON_TEXT_WRITERS with a reason."
    )
    stale = (REDACTING_WRITERS | NON_TEXT_WRITERS) - mutating
    assert not stale, f"classified but no longer writing: {sorted(stale)}"


def test_database_file_contains_no_secret(tmp_path) -> None:
    """Columns can read clean while the value survives in a WAL page."""
    db_path = tmp_path / "boundary.db"
    store = Store(db_path)
    _write_everything(store)
    store.close()

    files = [db_path, *tmp_path.glob("boundary.db-*")]
    blob = b"".join(Path(path).read_bytes() for path in files)
    assert b"REDACTED" in blob, "nothing reached disk; the scan below is vacuous"
    assert CANARY.encode() not in blob, "canary survived in the database file"


def test_hostile_types_do_not_crash_ingestion(tmp_path) -> None:
    """External rows are not type-guaranteed.

    A SQLite-backed source can return a BLOB and a malformed log can carry a
    number where text is expected. `redact()` raises `TypeError` on both, so a
    redaction fix applied carelessly trades a leak for an ingestion crash.
    """
    store = Store(tmp_path / "types.db")
    try:
        store.start_run("run-1", 0)
        store.insert_router(
            "nine_router", "n1", 0, CANARY.encode(), 42, None, 3.14, 0.0, 1, 1,
            CANARY.encode(),
        )
        call_id = store.insert_agent_call(
            run_id="run-1", agent_type="t", started_ms=0, model=CANARY.encode()
        )
        store.insert_agent_event("run-1", "t", call_id, 0, "topic", "info", 42, None)
        store.finish_agent_call(call_id, 1, "ok", output_json=CANARY.encode())
        store.commit()
    finally:
        store.close()

    blob = b"".join(
        Path(p).read_bytes()
        for p in [tmp_path / "types.db", *tmp_path.glob("types.db-*")]
    )
    assert CANARY.encode() not in blob, "bytes bypassed redaction into a BLOB"


def test_every_redacting_writer_is_actually_exercised() -> None:
    """Classification is not coverage.

    `REDACTING_WRITERS` is a hand-maintained list; a writer can be added to it
    and still never receive a canary, which would leave the leak tests passing
    for it by omission. This reads the calls `_write_everything` really makes.

    The AST scan behind `test_every_writer_is_classified` is a literal-SQL
    heuristic: it sees `"INSERT ..."` written inline, and would miss SQL
    assembled from variables or built in a helper. Named honestly rather than
    presented as exhaustive.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_write_everything))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "store"
    }
    missing = REDACTING_WRITERS - called
    assert not missing, (
        f"classified as redacting but never exercised with a canary: "
        f"{sorted(missing)}"
    )
