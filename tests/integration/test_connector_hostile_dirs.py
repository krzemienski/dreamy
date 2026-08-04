"""A crafted harness directory must not abort ingestion.

The decoders raise on a malformed encoding rather than guessing a path — the
right answer at the boundary, since falling back to the deepest matched
ancestor would run `git` in a real but *wrong* repository and produce plausible
evidence attributed to the wrong project.

But an uncaught raise turns a path-traversal bug into an ingestion
denial-of-service: one crafted directory under a harness store would stop the
whole scan.

Three design notes, all learned by getting it wrong first:

- The hostile record deliberately carries **no** `cwd` field. An earlier draft
  gave it a safe one, so the record ingested through the native-`cwd` path
  whether or not the directory-decode catch ran — the test passed vacuously.
- Each assertion on a surviving sibling is paired with an assertion that the
  warning actually fired, so "no records at all" cannot read as success.
- The two harnesses use different record shapes (Claude: `uuid` + `type:
  "user"`; OMP: `id` + `type: "session"`/`"message"`). A shared fixture
  produced zero OMP records, which also passes for the wrong reason.
"""

from __future__ import annotations

import json

from dreamy.connectors.claude import ClaudeConnector
from dreamy.connectors.omp_pi import OmpConnector

TS = "2026-08-01T12:00:00Z"


def _claude_line(*, cwd: str | None, text: str = "hello") -> str:
    obj: dict[str, object] = {
        "type": "user",
        "uuid": "11111111-1111-1111-1111-111111111111",
        "timestamp": TS,
        "message": {"role": "user", "content": text},
    }
    if cwd is not None:
        obj["cwd"] = cwd
    return json.dumps(obj) + "\n"


def _omp_lines(*, cwd: str | None, text: str = "hello") -> str:
    session: dict[str, object] = {
        "type": "session",
        "id": "sess-1",
        "timestamp": TS,
        "model": "test-model",
    }
    if cwd is not None:
        session["cwd"] = cwd
    message = {
        "type": "message",
        "id": "msg-1",
        "timestamp": TS,
        "message": {"role": "user", "content": text},
    }
    return json.dumps(session) + "\n" + json.dumps(message) + "\n"


def test_claude_hostile_dir_does_not_abort_scan(tmp_path, capsys) -> None:
    root = tmp_path / "projects"
    hostile = root / "-tmp-.."
    hostile.mkdir(parents=True)
    # No cwd: the ONLY attribution route is the directory decode, which raises.
    (hostile / "bad.jsonl").write_text(_claude_line(cwd=None, text="hostile"))

    good = root / "-Users-someone-realproject"
    good.mkdir(parents=True)
    (good / "ok.jsonl").write_text(_claude_line(cwd="/Users/someone/realproject"))

    records = list(
        ClaudeConnector().scan(
            watermark_ms=0, lookback_days=36500, path_override=str(root)
        )
    )

    err = capsys.readouterr().err
    assert "unusable project directory" in err, (
        f"the per-file catch must have run; stderr was {err!r}"
    )
    good_records = [r for r in records if "realproject" in r.project_path]
    assert len(good_records) >= 1, (
        "the legitimate session must still be ingested; a zero-record scan "
        "would make 'no crash' trivially true"
    )
    for record in records:
        assert ".." not in record.project_path.split("/"), record.project_path
    for record in records:
        if "hostile" in record.content_excerpt:
            assert record.project_path == "", (
                f"hostile record must be unattributed, got {record.project_path!r}"
            )


def test_omp_hostile_dir_does_not_abort_scan(tmp_path, capsys) -> None:
    root = tmp_path / "sessions"
    hostile = root / "-.."
    hostile.mkdir(parents=True)
    (hostile / "bad.jsonl").write_text(_omp_lines(cwd=None, text="hostile"))

    good = root / "-realproject"
    good.mkdir(parents=True)
    (good / "ok.jsonl").write_text(_omp_lines(cwd="/home/u/realproject"))

    records = list(
        OmpConnector().scan(
            watermark_ms=0, lookback_days=36500, path_override=str(root)
        )
    )

    err = capsys.readouterr().err
    assert "unusable session directory" in err, (
        f"the per-file catch must have run; stderr was {err!r}"
    )
    good_records = [r for r in records if "realproject" in r.project_path]
    assert len(good_records) >= 2, (
        "both the session_start and message records from the good sibling "
        f"must survive; got {len(good_records)}"
    )
    for record in records:
        assert ".." not in record.project_path.split("/"), record.project_path


def test_hostile_cwd_field_is_not_trusted(tmp_path) -> None:
    """The native `cwd` field is attacker-controlled too.

    A transcript can carry any string here, and it reaches a git subprocess as
    `cwd`. Validating only the directory decode would leave this open.
    """
    root = tmp_path / "projects"
    project = root / "-Users-someone-proj"
    project.mkdir(parents=True)
    (project / "s.jsonl").write_text(_claude_line(cwd="/safe/../../etc"))

    records = list(
        ClaudeConnector().scan(
            watermark_ms=0, lookback_days=36500, path_override=str(root)
        )
    )

    assert records, "the session itself must still be read"
    for record in records:
        assert ".." not in record.project_path.split("/"), record.project_path
