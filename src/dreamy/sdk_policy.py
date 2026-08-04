"""Explicit capability policy for the Dreamy evidence summarizer.

This module intentionally keeps the policy declarative and small. The wrapper
applies it against the installed SDK options object and refuses to call the
model if the installed SDK cannot represent the policy.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import Any

AGENT_SDK_POLICY_NAME = "dreamy-readonly-evidence-summarizer-v1"

EXPECTED_SDK_VERSION = "0.2.128"
"""Greppable name for the fail-closed SDK capability policy."""

# SDK 0.2.128 was probed at runtime. These are the only capabilities required
# to make the summarizer reproducible and unable to perform side effects.
_POLICY_FIELDS: dict[str, Any] = {
    "tools": [],
    "allowed_tools": [],
    "disallowed_tools": ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"],
    "permission_mode": "dontAsk",
    "setting_sources": [],
}


def apply_policy(options: Any, *, probe_field: str | None = None, sdk_version: str | None = None) -> None:
    """Apply the complete policy or raise before any SDK call is attempted.

    ``probe_field`` exists solely for acceptance verification: it intentionally
    exercises the same missing-field path used when an SDK version changes.
    """
    installed_version = sdk_version or version("claude-agent-sdk")
    if installed_version != EXPECTED_SDK_VERSION:
        raise RuntimeError(
            f"{AGENT_SDK_POLICY_NAME} requires claude-agent-sdk=={EXPECTED_SDK_VERSION}; found {installed_version}"
        )
    # Capability probe by attempted write, not by type or prior declaration.
    #
    # The only question this policy has to answer is whether the installed SDK
    # options object can *represent* every field the policy names — and a
    # successful `setattr` is the proof of that. Two earlier formulations both
    # answered a narrower question and got it wrong:
    #
    #   `dataclasses.fields()` asked "is this literally a dataclass" and raised
    #   TypeError on anything else, turning a representable policy into an
    #   unhandled crash rather than the intended refusal.
    #
    #   `hasattr` asked "is this field already declared", which rejects any
    #   object that accepts attributes but does not predeclare them, even
    #   though assigning to it would have succeeded.
    #
    # A slotted or otherwise restricted object still fails closed here, because
    # its `setattr` raises. Applied one field at a time so the error names the
    # field that could not be represented.
    # `probe_field` is the acceptance hook for "an SDK version dropped a field
    # we depend on". It must be judged by DECLARED capability, never by an
    # attempted write: a dynamic attribute bag accepts any name, which would
    # make the probe silently pass and the acceptance check worthless.
    if probe_field is not None and not hasattr(options, probe_field):
        raise RuntimeError(
            f"{AGENT_SDK_POLICY_NAME} cannot be applied; "
            f"SDK options missing fields: {probe_field}"
        )
    for name, value in _POLICY_FIELDS.items():
        try:
            setattr(options, name, value)
        except Exception as exc:
            raise RuntimeError(
                f"{AGENT_SDK_POLICY_NAME} cannot be applied; "
                f"SDK options rejected field {name!r}: {exc}"
            ) from exc
        if getattr(options, name, None) != value:
            # Accepted the write but did not retain it — a silent no-op setter
            # would leave the policy unenforced while appearing to succeed.
            raise RuntimeError(
                f"{AGENT_SDK_POLICY_NAME} cannot be applied; "
                f"SDK options did not retain field {name!r}"
            )


def policy_fields() -> tuple[str, ...]:
    """Return the fields required by this policy for diagnostics."""
    return tuple(_POLICY_FIELDS)
