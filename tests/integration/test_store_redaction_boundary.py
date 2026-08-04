"""SAFE-002: no model- or transcript-derived text reaches SQLite unredacted.

Redaction is applied at the `Store` write methods rather than at their callers.
The reasoning is asymmetric: one boundary cannot be forgotten, but every future
call site can, and a forgotten call site writes credentials to a database file
that then sits on disk indefinitely.

An earlier fix covered `finish_agent_call` only. An independent review found
three sibling writers still bypassing it — this test covers every column any of
them writes, so adding a fourth writer without redaction fails here.

The assertion is made at the SQLite file level as well as the column level: a
column can read clean while the value survives in a WAL page or a freelist page
of the same file.
"""

from __future__ import annotations

from pathlib import Path

from dreamy.store import Store

# Shaped like a real credential so the redactor's patterns actually engage; a
# generic token such as "SECRET" could pass by never matching anything.
CANARY = "sk-ant-api03-CANARYLEAKPROBE0000000000000000000000000000000000"

# A non-secret marker written alongside the canary. Redaction leaves it intact,
# so its presence proves the write actually landed — without it, every absence
# assertion here would also hold for a store that persists nothing.
SENTINEL = "dreamy-persistence-sentinel"

# Per-column marker. A single shared sentinel proves only that SOME write
# landed: with one marker, dropping just `insert_agent_event` still leaves the
# findings rows on disk and the byte scan passes. Each column carries its own
# so every writer is independently pinned.
def _marker(table: str, column: str) -> str:
    return f"{SENTINEL}-{table}-{column}"


def _populate(db_path: Path) -> Store:
    store = Store(db_path)
    run_id = "run-canary"
    store.start_run(run_id, 0)

    call_id = store.insert_agent_call(
        run_id=run_id,
        agent_type="probe",
        model="test-model",
        started_ms=0,
        input_fingerprint="fp",
    )
    store.insert_agent_event(
        run_id,
        "probe",
        call_id,
        0,
        "topic",
        "info",
        f"{_marker('agent_events', 'message')} leaked {CANARY}",
        f'{{"k":"{CANARY}","m":"{_marker("agent_events", "fields_json")}"}}',
    )
    store.finish_agent_call(
        call_id,
        1,
        "ok",
        output_json=f'{{"o":"{CANARY}","m":"{_marker("agent_calls", "output_json")}"}}',
        error_text=f"{_marker('agent_calls', 'error_text')} err {CANARY}",
    )

    project_id = store.upsert_project("/tmp/probe-project", "probe-project", 0, 0)
    store.insert_finding_with_provenance(
        project_id,
        {
            "title": f"{_marker('findings', 'title')} title {CANARY}",
            "detail": f"{_marker('findings', 'detail')} detail {CANARY}",
            "evidence": {"quote": CANARY, "marker": _marker("findings", "evidence_json")},
            "category": "research_support",
        },
        provenance="agent",
    )
    store.commit()
    return store


COLUMNS = (
    ("agent_events", ("message", "fields_json")),
    ("agent_calls", ("output_json", "error_text")),
    ("findings", ("title", "detail", "evidence_json")),
)


def test_no_free_text_column_persists_a_secret(tmp_path) -> None:
    store = _populate(tmp_path / "probe.db")
    try:
        for table, columns in COLUMNS:
            for column in columns:
                row = store.conn.execute(f"SELECT {column} FROM {table}").fetchone()
                assert row is not None, f"{table}.{column}: no row written"
                value = row[0] or ""
                assert CANARY not in value, f"{table}.{column} leaked: {value!r}"
                assert _marker(table, column) in value, (
                    f"{table}.{column}: sentinel missing — the write did not "
                    f"land, so the leak assertion above is vacuous: {value!r}"
                )
                assert "REDACTED" in value, (
                    f"{table}.{column} was written without passing through the "
                    f"redactor: {value!r}"
                )
    finally:
        store.close()


def test_database_file_bytes_contain_no_secret(tmp_path) -> None:
    """A column can read clean while the raw value survives on disk.

    The absence assertion is paired with a positive control. On its own it
    passes against a store that persists NOTHING — proven by mutation: dropping
    the payload before `execute()` left this test green while the column test
    failed. An absence check with no proof the write happened is vacuous, which
    is this codebase's recurring failure mode.

    The sentinel is a non-secret substring of the same values, so it survives
    redaction and appears on disk exactly when the write actually landed.
    """
    db_path = tmp_path / "probe.db"
    store = _populate(db_path)
    store.close()

    # Includes -wal and -shm: the DB is in WAL mode, so a value can be absent
    # from the main file while sitting in the write-ahead log.
    files = [db_path, *tmp_path.glob("probe.db-*")]
    blob = b"".join(path.read_bytes() for path in files)

    for table, columns in COLUMNS:
        for column in columns:
            assert _marker(table, column).encode() in blob, (
                f"{table}.{column} never reached disk — that writer persisted "
                "nothing, so the absence assertion below is vacuous for it"
            )
    for path in files:
        assert CANARY.encode() not in path.read_bytes(), f"{path.name} leaked"


def test_finding_id_is_derived_from_redacted_text(tmp_path) -> None:
    """Two findings differing only in secret material must collide.

    If the id were derived from raw text, the same finding seen twice with
    rotated credentials would persist as two rows keyed by secret bytes.
    """
    store = Store(tmp_path / "ids.db")
    try:
        project_id = store.upsert_project("/tmp/p", "p", 0, 0)
        first = store.insert_finding_with_provenance(
            project_id,
            {"title": f"t {CANARY}", "detail": "d", "evidence": {"q": CANARY}},
            provenance="agent",
        )
        second = store.insert_finding_with_provenance(
            project_id,
            {
                "title": "t sk-ant-api03-DIFFERENTSECRET000000000000000000000000000000",
                "detail": "d",
                "evidence": {"q": "sk-ant-api03-DIFFERENTSECRET000000000000000000000000000000"},
            },
            provenance="agent",
        )
        assert first == second, (
            "ids differ, so they were derived from unredacted text"
        )
        count = store.conn.execute("SELECT count(*) FROM findings").fetchone()[0]
        assert count == 1, f"expected one deduplicated row, got {count}"
    finally:
        # Throwaway probe DB: the assertions read through the open connection,
        # so nothing here needs to survive the close. Stated explicitly
        # because `close()` now refuses to discard pending writes silently.
        store.close(discard=True)
