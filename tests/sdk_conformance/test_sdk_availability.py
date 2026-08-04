"""Sentinel so a targeted conformance run never exits 5.

`pytest.importorskip` at module scope skips the whole file. A run scoped to this
directory would then collect ZERO tests, which pytest reports as exit code 5 --
a distinct failure code, not a skip. A per-suite CI job invoking
`pytest tests/sdk_conformance/` on a machine without the `agent` extra would
fail even though nothing is wrong.

This module is always collected: it passes when the SDK is present and skips
with a reason when it is not, so the directory always yields a result.
"""

from __future__ import annotations

import importlib.util

import pytest

SDK_INSTALLED = importlib.util.find_spec("claude_agent_sdk") is not None


def test_sdk_availability_is_reported() -> None:
    """Always collected; skips rather than leaving the directory empty."""
    if not SDK_INSTALLED:
        pytest.skip(
            "agent extra not installed; SDK conformance requires "
            "claude-agent-sdk (see docs/research/SDK-VERSION-LOCK.md)"
        )
    import claude_agent_sdk

    assert claude_agent_sdk.__name__ == "claude_agent_sdk"
