"""Every scrub target must exist in the schema it claims to protect.

`SCRUBBABLE_COLUMNS` named `intent_episodes.intent_text`. That column has
never existed — the real one is `original_intent` — and `scrub_database`
skipped unknown columns silently, so the field holding raw first-user-turn
text was never scrubbed while the run reported success.

A typo in a security control is invisible precisely when it matters. These
tests make the list self-checking: a name that does not match the schema
fails here rather than at the next incident.
"""
from __future__ import annotations

import pytest

from dreamy.redact import SCRUBBABLE_COLUMNS, scrub_database
from dreamy.store import Store


@pytest.fixture()
def live_schema(tmp_path):
    """A real migrated database — the schema the scrub actually runs against."""
    store = Store(tmp_path / "schema.db")
    yield store.conn
    store.close()


def test_every_scrub_target_exists(live_schema) -> None:
    """No entry may name a table/column absent from a migrated database."""
    tables = {
        r[0] for r in live_schema.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = []
    for table, column in SCRUBBABLE_COLUMNS:
        if table not in tables:
            missing.append(f"{table} (table absent)")
            continue
        cols = {r[1] for r in live_schema.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            missing.append(f"{table}.{column} (column absent; has {sorted(cols)})")
    assert not missing, "scrub targets not in schema: " + "; ".join(missing)


def test_free_text_columns_are_covered(live_schema) -> None:
    """The columns that can carry a credential must be listed.

    Named explicitly rather than discovered: a scrub that rewrote every TEXT
    column would also rewrite ids and hashes. But the free-text ones are not
    optional, and `original_intent` is the specific field the typo missed —
    it stores a raw user turn, which is exactly where a pasted key lands.
    """
    listed = {f"{t}.{c}" for t, c in SCRUBBABLE_COLUMNS}
    required = {
        "turns.content_fingerprint",
        "turns.error_text",
        "turns.raw_meta_json",
        "intent_episodes.original_intent",
        "findings.detail",
        "agent_calls.output_json",
    }
    assert required <= listed, f"unprotected free-text columns: {required - listed}"


def test_unknown_column_refuses_rather_than_skipping(live_schema, monkeypatch) -> None:
    """A bad target must fail loudly.

    This is the actual defect: the skip was silent, so a partial scrub
    reported success. Mutating the list to reintroduce the original typo
    must now raise.
    """
    import dreamy.redact as redact_mod

    monkeypatch.setattr(
        redact_mod,
        "SCRUBBABLE_COLUMNS",
        (("intent_episodes", "intent_text"),),  # the historical typo
    )
    with pytest.raises(ValueError, match="scrub targets do not exist"):
        scrub_database(live_schema)


def test_absent_table_is_skipped_quietly(live_schema, monkeypatch) -> None:
    """A missing TABLE is legitimate — older databases predate migrations.

    Only a missing column in a table that DOES exist is a list bug, so this
    boundary must not become an error or the scrub would break on every old
    database it is meant to clean.
    """
    import dreamy.redact as redact_mod

    monkeypatch.setattr(
        redact_mod,
        "SCRUBBABLE_COLUMNS",
        (("table_from_a_future_migration", "some_col"),),
    )
    assert scrub_database(live_schema) == {}


def test_scrub_actually_cleans_original_intent(live_schema) -> None:
    """End-to-end on the column the typo missed, with a synthetic value."""
    synthetic = "sk-aaaa-bbbbbb-ccccc"
    live_schema.execute(
        "INSERT INTO projects(id, path, name, first_seen_ms, last_seen_ms) "
        "VALUES('p1','/tmp/p','p',0,0)"
    )
    live_schema.execute(
        "INSERT INTO intent_episodes"
        "(id, project_id, started_ms, ended_ms, original_intent, completion_status) "
        "VALUES('ep1','p1',0,0,?,'in_progress')",
        (f'paste this: {{"anthropic_auth_token": "{synthetic}"}}',),
    )
    live_schema.commit()

    changed = scrub_database(live_schema)
    assert changed.get("intent_episodes.original_intent") == 1

    # `id` is TEXT PRIMARY KEY, so it must be supplied — omitting it stores
    # NULL and any later lookup by id silently matches nothing.
    row = live_schema.execute(
        "SELECT original_intent FROM intent_episodes WHERE id='ep1'"
    ).fetchone()
    assert row is not None, "row 'ep1' not found — the insert did not set id"
    stored = row[0]
    assert synthetic not in stored
    assert "REDACTED_SECRET" in stored


@pytest.mark.parametrize(
    "column",
    ["acceptance_criteria_json", "completion_evidence_json"],
    ids=["acceptance-criteria", "completion-evidence"],
)
def test_scrub_cleans_newly_added_json_columns(live_schema, column: str) -> None:
    """The two columns added alongside the `original_intent` fix.

    Both were added as security targets in the same change that corrected
    the typo, and neither had a test. An untested security target is the
    same failure mode as the typo: it looks covered.

    Both hold JSON derived from session content, so a credential quoted in
    a user turn can reach them the same way it reached
    `content_fingerprint`.
    """
    synthetic = "sk-dddd-eeeeee-fffff"
    # Row identity is per-parameter, with an EXPLICIT id.
    #
    # Both runs previously wrote `project_id='p2'` and read back with
    # `WHERE project_id='p2'`, identifying a row by a value the other run
    # also writes. Switching to `cur.lastrowid` did not fix it either:
    # `intent_episodes.id` is `TEXT PRIMARY KEY`, so an INSERT that omits it
    # stores NULL while `lastrowid` returns the internal rowid — and
    # `WHERE id=<rowid>` matches nothing. The assertion still passed, which
    # is exactly the danger: it was reading a row it had not identified.
    project_id = f"proj-{column}"
    episode_id = f"ep-{column}"
    live_schema.execute(
        "INSERT INTO projects(id, path, name, first_seen_ms, last_seen_ms) "
        "VALUES(?,?,?,0,0)",
        (project_id, f"/tmp/{project_id}", project_id),
    )
    live_schema.execute(
        f"INSERT INTO intent_episodes"
        f"(id, project_id, started_ms, ended_ms, original_intent, "
        f"completion_status, {column}) VALUES(?,?,0,0,'intent','in_progress',?)",
        (episode_id, project_id, f'{{"note": "token was {synthetic}"}}'),
    )
    live_schema.commit()

    changed = scrub_database(live_schema)
    assert changed.get(f"intent_episodes.{column}") == 1

    row = live_schema.execute(
        f"SELECT {column} FROM intent_episodes WHERE id=?", (episode_id,)
    ).fetchone()
    assert row is not None, f"row {episode_id!r} not found — identity is wrong"
    stored = row[0]
    assert synthetic not in stored
    assert "REDACTED_SECRET" in stored


def test_scrub_erases_bytes_not_just_rows(tmp_path) -> None:
    """An UPDATE alone leaves the secret readable in the file.

    SQLite writes the new value elsewhere and keeps the pre-image in the old
    page, copying it to the WAL as well. A file scan straight after an
    UPDATE still finds the credential, so the scrub commits, arms
    `secure_delete`, truncates the WAL, and VACUUMs.

    This asserts on the BYTES of the database file, because that is the
    property that matters and the one a row-level assertion cannot see.
    """
    synthetic = "sk-gggg-hhhhhh-iiiii"
    db = tmp_path / "erase.db"
    store = Store(db)
    conn = store.conn
    conn.execute(
        "INSERT INTO projects(id, path, name, first_seen_ms, last_seen_ms) "
        "VALUES('p3','/tmp/p3','p3',0,0)"
    )
    # Enough rows that the value lands on pages which must be rewritten,
    # not just held in a single freshly-allocated one.
    #
    # Each row gets an explicit id: `intent_episodes.id` is TEXT PRIMARY KEY,
    # so omitting it stores NULL. 200 NULL "primary keys" is not what this
    # fixture is meant to model, and it would mask a real UNIQUE failure.
    for i in range(200):
        conn.execute(
            "INSERT INTO intent_episodes"
            "(id, project_id, started_ms, ended_ms, original_intent, "
            "completion_status) VALUES(?,'p3',0,0,?,'in_progress')",
            (f"ep3-{i:03d}", f'turn {i}: export ANTHROPIC_AUTH_TOKEN="{synthetic}"'),
        )
    conn.commit()
    # WAL mode: committed rows live in `-wal` until a checkpoint folds them
    # into the main file, so "is the secret on disk" must consider all three
    # files. Reading only `db` here failed this assertion while the secret
    # was plainly present in `db-wal` — the fixture would have proven
    # nothing.
    def _on_disk() -> bytes:
        blob = b""
        for suffix in ("", "-wal", "-shm"):
            f = db.with_name(db.name + suffix)
            if f.is_file():
                blob += f.read_bytes()
        return blob

    assert synthetic.encode() in _on_disk(), "fixture must actually persist the secret"

    changed = scrub_database(conn)
    assert changed.get("intent_episodes.original_intent") == 200
    store.close()

    assert synthetic.encode() not in _on_disk(), (
        "secret still readable in the database files after scrub — rows were "
        "rewritten but the pre-image bytes were not erased"
    )