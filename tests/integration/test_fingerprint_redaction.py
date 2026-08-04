"""SAFE-002: `turns.content_fingerprint` must cross the redaction boundary.

This closes a real leak. A live Anthropic auth token was found persisted in
`turns.content_fingerprint` on the production database. Two independent
defects combined:

1. `correlate.py` bound `normalize_fingerprint(rec.content_excerpt)` — the
   ONLY argument on that `insert_turn` call without a `redact()` wrapper,
   while `file_paths_json`, `error_text`, and `raw_meta_json` all had one.
2. The `sk-` pattern required a 20+ character body of `[A-Za-z0-9]`. Real
   keys have a 19-character body containing hyphens, so it never matched.

Either defect alone was sufficient. The canary test did not catch it: the
canary is `sk-ant-` shaped and matches a different rule than the one that
was broken — the same blind spot recorded as D11.

Every value here is synthetic. No real credential appears in this file.
"""
from __future__ import annotations

import re

import pytest

from dreamy.correlate import normalize_fingerprint
from dreamy.redact import redact

# Synthetic, matching the SHAPE of the leaked key: `sk-` + hyphenated body.
SYNTHETIC_KEY = "sk-aaaa-bbbbbb-ccccc"
SYNTHETIC_LONG = "sk-" + "z" * 32


@pytest.mark.parametrize(
    "raw",
    [
        SYNTHETIC_KEY,
        f'"anthropic_auth_token": "{SYNTHETIC_KEY}"',
        f"use these vars {{ 'env': {{ 'anthropic_auth_token': '{SYNTHETIC_KEY}' }} }}",
        SYNTHETIC_LONG,
    ],
)
def test_fingerprint_never_carries_a_secret(raw: str) -> None:
    """The exact composition `correlate.py` persists must be clean.

    Asserted on the composed expression rather than on `redact()` alone: the
    leak was an ordering/omission bug at the call site, so testing the
    redactor in isolation would have passed while the product still leaked.
    """
    fingerprint = normalize_fingerprint(redact(raw))
    assert "sk-aaaa" not in fingerprint
    assert "sk-zzzz" not in fingerprint
    assert not re.search(r"sk-[A-Za-z0-9_\-]{12,}", fingerprint), fingerprint


def test_short_body_key_is_caught() -> None:
    """The specific off-by-one that shipped: a 19-character body.

    The previous floor of 20 let this through, and the leaked key was exactly
    this length.
    """
    key = "sk-" + "a" * 19
    assert len(key) - 3 == 19, "fixture must reproduce the 19-char body"
    assert redact(key) != key


def test_hyphenated_body_is_caught() -> None:
    """The second half: `[A-Za-z0-9]` excluded the separators real keys use."""
    assert "-" in SYNTHETIC_KEY[3:], "fixture must contain hyphens in the body"
    assert redact(SYNTHETIC_KEY) != SYNTHETIC_KEY


def test_json_quoted_key_is_caught() -> None:
    """Transcripts carry JSON, where a closing quote precedes the colon.

    `token\\s*[=:]` never matched `"anthropic_auth_token":` for that reason.
    """
    blob = f'{{"anthropic_auth_token": "{SYNTHETIC_KEY}"}}'
    assert SYNTHETIC_KEY not in redact(blob)


@pytest.mark.parametrize(
    "benign",
    [
        "a" * 64,                      # a SHA-256 digest; this pipeline stores many
        "we should ask-someone about this",
        "sk-ab",                       # too short to be a credential
        # Word-boundary cases. These are the regression that matters: widening
        # the `sk-` body to accept hyphens made it match INSIDE ordinary words
        # (ta`sk-`, di`sk-`, fla`sk-`), and the widened class then consumed the
        # rest, so `task-architect-skill-name` became `taREDACTED_SECRET`.
        # The first suite passed anyway — bare `task-architect` is 14 chars,
        # just under the floor — so only a longer, realistic identifier
        # exposes it.
        "task-architect-skill-name",
        "disk-usage-monitor-v2",
        "flask-application-server",
        "mask-position-x is a css property",
        "~/.claude/skills/task-architect/SKILL.md",
    ],
)
def test_benign_content_survives(benign: str) -> None:
    """A redactor that shreds real content is its own defect.

    Digests especially: fingerprints, stable_hash values, and manifests are
    full of them, and over-broad matching would corrupt the data the product
    exists to produce.
    """
    assert redact(benign) == benign
