"""Conformance checks against the INSTALLED Agent SDK build.

Requirement: `SDK-001` — the installed API must be inventoried and pinned.

These assert the contract `docs/research/SDK-CAPABILITY-MATRIX.md` records. They
are not unit tests of Dreamy; they detect the SDK moving under us. A capability
matrix that nothing re-checks is a snapshot of a build that may no longer exist.

No mocks: every assertion reads the real installed package. Skipped wholesale
when the `agent` extra is absent, because a missing optional dependency is a
degraded mode (`SDK-003`), not a failure.
"""

from __future__ import annotations

import dataclasses

import pytest

sdk = pytest.importorskip(
    "claude_agent_sdk", reason="agent extra not installed; SDK conformance skipped"
)

pytestmark = pytest.mark.sdk

# Pinned in pyproject.toml and hash-locked in config/requirements-agent.lock.
PINNED_VERSION = "0.2.128"

# Parsed from the installed ClaudeAgentOptions.hooks annotation, recorded in
# SDK-VERSION-LOCK.md. Authoritative for OBS-004.
EXPECTED_HOOK_EVENTS = {
    "Notification",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "PreToolUse",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "UserPromptSubmit",
}


def _option_fields() -> dict[str, dataclasses.Field]:
    return {f.name: f for f in dataclasses.fields(sdk.ClaudeAgentOptions)}


def test_installed_version_matches_the_pin():
    """A version drift invalidates every disposition in the matrix.

    Failure here is not a broken test; it means the capability review must be
    re-run against the new build before any SDK-* gate may be claimed.
    """
    import importlib.metadata as md

    assert md.version("claude-agent-sdk") == PINNED_VERSION


def test_hook_events_match_the_recorded_set():
    """OBS-004 names these events; a silent addition or removal breaks it."""
    import typing

    hooks_field = _option_fields()["hooks"]
    found = {
        arg
        for arg in _literal_values(hooks_field.type)
        if isinstance(arg, str)
    }
    assert found == EXPECTED_HOOK_EVENTS, (
        f"hook events drifted: added {sorted(found - EXPECTED_HOOK_EVENTS)}, "
        f"removed {sorted(EXPECTED_HOOK_EVENTS - found)}"
    )
    del typing


def _literal_values(annotation: object) -> set[object]:
    """Collect Literal members anywhere inside a nested annotation."""
    import typing

    found: set[object] = set()
    stack = [annotation]
    seen = 0
    while stack and seen < 200:
        seen += 1
        node = stack.pop()
        if isinstance(node, str):
            # Some annotations arrive unevaluated; parse the Literal members out.
            import re

            found.update(re.findall(r"'([A-Za-z]+)'", node))
            continue
        if typing.get_origin(node) is typing.Literal:
            found.update(typing.get_args(node))
            continue
        stack.extend(typing.get_args(node))
    return found


def test_permission_modes_include_dontask():
    """PERMISSION-POLICY selects `dontAsk`; its absence would invalidate that."""
    modes = _literal_values(_option_fields()["permission_mode"].type)
    assert "dontAsk" in modes
    assert "bypassPermissions" in modes, "expected the mode the policy forbids"


def test_required_options_are_present():
    """Every option a `required` disposition depends on."""
    fields = _option_fields()
    for name in (
        "permission_mode",
        "allowed_tools",
        "disallowed_tools",
        "can_use_tool",
        "hooks",
        "output_format",
        "max_budget_usd",
        "mcp_servers",
        "agents",
        "skills",
        "setting_sources",
        "system_prompt",
    ):
        assert name in fields, f"required option {name!r} absent from this build"


def test_prohibited_options_still_exist_and_stay_off_by_default():
    """Prohibited capabilities must be *available yet unused*.

    If one vanished, the prohibition would be moot and the matrix row stale. If
    one defaulted to on, Dreamy would inherit it without opting in.
    """
    fields = _option_fields()
    assert fields["enable_file_checkpointing"].default is False
    assert "plugins" in fields


def test_checkpointing_is_not_coupled_to_write_permission():
    """The prohibition is Dreamy's contract, not an SDK constraint.

    An earlier review claimed the SDK requires `acceptEdits` for checkpointing,
    reasoning from every documented example pairing them. That is convention,
    not coupling — this asserts the distinction so the matrix stays accurate.
    """
    options = sdk.ClaudeAgentOptions(
        enable_file_checkpointing=True,
        permission_mode="dontAsk",
        allowed_tools=["Read"],
        disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"],
    )
    assert options.enable_file_checkpointing is True


def test_subagents_expose_the_escalation_surface():
    """AgentDefinition can override permissions and add MCP servers.

    Documented in the permissions reference; asserted here because Dreamy's
    safety design depends on it being true. A "safe parent" does not bound its
    children, so every AgentDefinition must set `tools` and never set
    `permissionMode` or `mcpServers`.
    """
    fields = {f.name for f in dataclasses.fields(sdk.AgentDefinition)}
    assert {"permissionMode", "mcpServers", "tools", "disallowedTools"} <= fields
    modes = _literal_values(
        {f.name: f for f in dataclasses.fields(sdk.AgentDefinition)}[
            "permissionMode"
        ].type
    )
    assert "bypassPermissions" in modes


def test_todo_state_has_no_dedicated_export():
    """Row 15 resolution, and a guard against a naming trap.

    Todo state streams as ToolUseBlock content. The `Task*` exports belong to the
    unrelated background-task family; reading `TaskUpdatedMessage` as todo state
    would be exactly the name-based inference the review exists to prevent.
    """
    exports = {n for n in dir(sdk) if not n.startswith("_")}
    assert not {n for n in exports if n.startswith("Todo")}
    assert "TaskUpdatedMessage" in exports  # background tasks, not todos


def test_result_message_carries_the_cost_fields():
    """SDK-014 reads cost from ResultMessage rather than estimating."""
    fields = {f.name for f in dataclasses.fields(sdk.ResultMessage)}
    assert {"total_cost_usd", "usage", "model_usage"} <= fields
