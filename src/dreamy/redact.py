"""Secret redaction at the ingestion boundary."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_FULL_REPLACE = [
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}", re.I),
    # Two corrections, both from real defects.
    #
    # Body allows hyphens/underscores and the floor is 12, not 20: a live key
    # was found persisted with a 19-character hyphenated body (shape
    # `sk-XXXX-YYYYYY-ZZZZZ`), which the old `sk-[A-Za-z0-9]{20,}` missed
    # twice over — too long a floor, and a class excluding its separators.
    #
    # `\b` is load-bearing. Without it `sk-` matches INSIDE ordinary words —
    # ta`sk-`architect, di`sk-`usage, fla`sk-`app — and the widened body then
    # swallows the rest, so `task-architect-skill-name` redacted to
    # `taREDACTED_SECRET`. That silently corrupts skill names, paths, and
    # prose: a redactor that destroys real content is its own defect.
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}", re.I),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", re.I),
    re.compile(r"tok_[A-Za-z0-9]{20,}", re.I),
    re.compile(r"AKIA[0-9A-Z]{16}", re.I),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}", re.I),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----", re.I),
    re.compile(r"mongodb(\+srv)?://[^\s'\"]+", re.I),
    re.compile(r"postgres(ql)?://[^\s'\"]+", re.I),
]

# Keyword-anchored forms. Each tolerates an optional closing quote before the
# separator: transcripts carry JSON, where the key is `"anthropic_auth_token":`
# and a bare `token\s*[=:]` never matches — the quote sits between them. That
# gap let a live credential through in a JSON env block.
_PREFIX_KEEP = [
    re.compile(r"(?i)(password\"?\s*[=:]\s*\"?)([^\s'\"]{8,})"),
    re.compile(r"(?i)(secret\"?\s*[=:]\s*\"?)([^\s'\"]{8,})"),
    re.compile(r"(?i)(token\"?\s*[=:]\s*\"?)([^\s'\"]{8,})"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[=:]\s*\"?)([^\s'\"]{8,})"),
]

CANARY = "sk-ant-test-PLANTED-SECRET-0123456789abcdef"

# --- Known-value redaction (D11) ------------------------------------------
#
# Every pattern above is anchored on a literal prefix (sk-, Bearer, tok_, AKIA,
# gh?_) or on a preceding keyword (token=, api_key:). A credential carrying
# neither survives verbatim. The 9router CLI secret is exactly that: 64
# characters of bare lowercase hex. Unanchored, it reached the product's
# state.db and state.db-wal (VG-S2 iso_token_hits, defect D11) while canary
# redaction still reported success — the canary is sk-ant-shaped, so it was
# caught by a rule the real token never touches.
#
# A pattern cannot fix this. 64-char lowercase hex is byte-indistinguishable
# from a SHA-256 digest, and this pipeline stores those everywhere — content
# fingerprints, artifact stable_hash values, manifests. Any regex broad enough
# to catch the token also shreds legitimate content.
#
# So match on the value, not the shape. The credential is read from its known
# locations and stripped by exact occurrence: deterministic, and zero false
# positives by construction.
#
# Secrets are held in memory only, never logged, and never written anywhere.

_SECRET_SOURCE_FILES = (
    "~/.9router/auth/cli-secret",
)

_SECRET_ENV_VARS = (
    "NINEROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
)

# Below this length, a "secret" is too short to strip safely — it would collide
# with ordinary words and corrupt real content.
_MIN_SECRET_LEN = 16

_known_secrets: tuple[str, ...] | None = None


def _load_known_secrets() -> tuple[str, ...]:
    """Collect live credential values from their known locations.

    Cached after first call: this runs per ingested turn, and re-reading the
    filesystem on every call would dominate ingestion cost. Call
    reset_known_secrets() if a credential rotates mid-process.
    """
    found: list[str] = []

    for var in _SECRET_ENV_VARS:
        val = (os.environ.get(var) or "").strip()
        if len(val) >= _MIN_SECRET_LEN:
            found.append(val)

    for raw in _SECRET_SOURCE_FILES:
        try:
            p = Path(os.path.expanduser(raw))
            if p.is_file():
                val = p.read_text(errors="ignore").strip()
                if len(val) >= _MIN_SECRET_LEN:
                    found.append(val)
        except OSError:
            # An unreadable credential file must never break ingestion.
            continue

    # Longest first, so a secret that contains another is replaced whole
    # rather than being split around its substring.
    return tuple(sorted(set(found), key=len, reverse=True))


def reset_known_secrets() -> None:
    """Drop the cache so the next redact() re-reads credential sources."""
    global _known_secrets
    _known_secrets = None


def known_secret_count() -> int:
    """Number of live credentials being stripped. Never returns their values."""
    global _known_secrets
    if _known_secrets is None:
        _known_secrets = _load_known_secrets()
    return len(_known_secrets)


def redact(text: str) -> str:
    """Replace secret-shaped substrings with labels."""
    if not text:
        return text
    for pat in _FULL_REPLACE:
        text = pat.sub("REDACTED_SECRET", text)
    for pat in _PREFIX_KEEP:
        text = pat.sub(r"\1REDACTED_SECRET", text)

    # Known live credentials, matched by value (D11). Runs last so the anchored
    # patterns above claim their matches first and keep prefix-preserving form.
    global _known_secrets
    if _known_secrets is None:
        _known_secrets = _load_known_secrets()
    for secret in _known_secrets:
        if secret in text:
            text = text.replace(secret, "REDACTED_SECRET")
    return text


def redact_dict(obj: dict) -> dict[str, Any]:
    """Recursively redact string values in a dict."""
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if isinstance(v, str):
            out[k] = redact(v)
        elif isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, list):
            out[k] = [
                redact(vi) if isinstance(vi, str)
                else redact_dict(vi) if isinstance(vi, dict)
                else vi
                for vi in v
            ]
        else:
            out[k] = v
    return out


def redact_value(obj: Any) -> Any:
    """Recursively redact strings in list/tuple/set items and in dict keys+values.
    Dict keys are coerced to str and redacted; collisions resolve deterministically
    (REDACTED_SECRET, REDACTED_SECRET#2, …) in insertion order. Tuples/sets
    normalize to lists (sets sorted by repr) for JSON safety."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            rk = redact(str(k))
            if rk in out:
                # Probe against ACTUAL occupied keys, not a per-base counter, so a
                # literal "REDACTED_SECRET#2" already present cannot be overwritten.
                n = 2
                while f"{rk}#{n}" in out:
                    n += 1
                rk = f"{rk}#{n}"
            out[rk] = redact_value(v)
        return out
    if isinstance(obj, list):
        return [redact_value(v) for v in obj]
    if isinstance(obj, tuple):
        return [redact_value(v) for v in obj]
    if isinstance(obj, set):
        return [redact_value(v) for v in sorted(obj, key=repr)]
    return obj


def redact_text(value: Any) -> Any:
    """Redact a value bound for a free-text database column.

    `redact()` takes a `str` and raises `TypeError` on anything else, which is
    correct for its own contract but wrong at a storage boundary: those values
    are copied from external harness and router rows, so their types are not
    guaranteed. Calling `redact()` on them directly trades a leak for an
    ingestion crash.

    Handling by type:

    * `str` — redacted.
    * `bytes` / `bytearray` — decoded UTF-8 with `errors="replace"`, then
      redacted. A SQLite-backed source can hand back a BLOB, and SQLite stores
      those bytes verbatim; scanning the database file finds the secret intact.
      Decoding is lossy for genuinely binary data, but a free-text column is
      not where binary belongs, and a leaked credential is worse than a mangled
      byte.
    * `None`, `int`, `float`, `bool` — returned unchanged. These cannot carry
      secret text and SQLite stores them natively.
    * anything else — `repr()`, then redacted. A list or dict reaching a TEXT
      column is malformed input, but SQLite's adapter would raise on it, so it
      is rendered rather than allowed to abort ingestion.
    """
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, bytes | bytearray):
        return redact(bytes(value).decode("utf-8", errors="replace"))
    return redact(repr(value))


# Columns that hold free text copied from an external source, and are
# therefore the only places a credential can come to rest. Enumerated
# explicitly rather than discovered: a scrub that rewrites every TEXT column
# would also rewrite ids and hashes, and a silent id change is worse than the
# leak it was fixing.
SCRUBBABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("turns", "content_fingerprint"),
    ("turns", "error_text"),
    ("turns", "raw_meta_json"),
    ("turns", "file_paths_json"),
    ("sessions", "git_branch"),
    ("findings", "title"),
    ("findings", "detail"),
    ("findings", "evidence_json"),
    # `original_intent`, NOT `intent_text`. This list named a column that has
    # never existed, and `scrub_database` skips unknown columns silently — so
    # the one field holding raw first-user-turn text went unscrubbed while the
    # run reported success. A typo in a security control is invisible unless
    # the control verifies its own targets, which `verify_scrub_targets` now
    # does.
    ("intent_episodes", "original_intent"),
    ("intent_episodes", "acceptance_criteria_json"),
    ("intent_episodes", "completion_evidence_json"),
    ("agent_calls", "output_json"),
    ("agent_calls", "error_text"),
    ("project_git_snapshots", "status_porcelain"),
    ("project_git_snapshots", "recent_log_json"),
    ("project_git_snapshots", "diff_stat"),
    ("project_git_snapshots", "error"),
)


def scrub_database(conn) -> dict[str, int]:
    """Re-redact free-text columns already written to disk, and erase the bytes.

    A pattern fix only stops FUTURE ingestion. Rows persisted while a rule was
    broken keep their secret verbatim, so closing a leak requires rewriting
    what is already stored — this ran for real after a live auth token was
    found in `turns.content_fingerprint`.

    An `UPDATE` alone is NOT sufficient and it is worth being precise about
    why. SQLite writes the new value to a fresh location; the old page keeps
    the original bytes until reused, and the pre-image is additionally copied
    into the WAL. A file scan immediately after `UPDATE` still finds the
    credential intact. Erasure therefore needs, in order:

    1. `commit` — make the rewrite durable.
    2. `PRAGMA secure_delete=ON` — overwrite freed content instead of merely
       delisting it.
    3. `PRAGMA wal_checkpoint(TRUNCATE)` — fold the WAL into the main
       database and truncate it, so no pre-image survives in `-wal`.
    4. `VACUUM` — rebuild the file so freed pages carrying the old bytes are
       released rather than lingering as slack.

    `VACUUM` cannot run inside a transaction, so this function owns the
    commit rather than leaving it to the caller. Verification belongs AFTER
    this returns, against the bytes on disk.

    Rewrites only where `redact()` actually changes the value, so a clean
    database is a no-op. Returns per-column counts of rows changed; never
    returns or logs the secret itself.

    Callers must hold the run lock: this rewrites rows a concurrent run would
    otherwise be reading.
    """
    changed: dict[str, int] = {}
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    # A named target that does not exist is a BUG IN THIS LIST, not an absent
    # feature — and skipping it quietly is how `intent_episodes.intent_text`
    # (real column: `original_intent`) went unscrubbed while the scrub
    # reported success. A security control that cannot see its own typo
    # provides false assurance, which is worse than no control.
    #
    # A missing TABLE is different and legitimately skippable: older
    # databases predate later migrations. A missing COLUMN in a table that
    # does exist has no benign reading.
    unknown: list[str] = []
    for table, column in SCRUBBABLE_COLUMNS:
        if table not in existing:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            unknown.append(f"{table}.{column}")
    if unknown:
        raise ValueError(
            "scrub targets do not exist: " + ", ".join(sorted(unknown))
            + " — fix SCRUBBABLE_COLUMNS; refusing to report a partial scrub "
            "as success"
        )

    for table, column in SCRUBBABLE_COLUMNS:
        if table not in existing:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            continue
        n = 0
        rows = conn.execute(
            f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()
        for rowid, value in rows:
            cleaned = redact_text(value)
            if isinstance(cleaned, str) and cleaned != value:
                conn.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?", (cleaned, rowid)
                )
                n += 1
        if n:
            changed[f"{table}.{column}"] = n
    conn.commit()

    # Erase the pre-images. Order matters: secure_delete must be armed before
    # VACUUM rebuilds the file, and the WAL must be folded in first or the
    # rewritten pages never reach the main database.
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    return changed


def contains_canary(text: str) -> bool:
    """Check if the planted canary secret is present (for acceptance testing)."""
    return CANARY in text
