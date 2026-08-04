#!/usr/bin/env python3
"""SDK-001: inventory the INSTALLED Agent SDK surface.

This is pass 2 of the four-pass review mandated for SDK-002 dispositions:

  1. official documentation, read sequentially      (not automatable here)
  2. installed package exports and signatures       <- this script
  3. official repository examples                   (not automatable here)
  4. changelog                                      (not automatable here)

The output is evidence, not a disposition. A capability appearing here means the
symbol exists in the installed build; it does NOT mean Dreamy uses it, that it
behaves as documented, or that it has been conformance-tested. Only
docs/research/SDK-CAPABILITY-MATRIX.md assigns dispositions, and only after all
four passes are complete for that capability.

Usage:  python3 tools/sdk-inventory.py [--json]
"""

from __future__ import annotations

import dataclasses
import importlib.metadata as md
import importlib.util
import inspect
import json
import platform
import re
import shutil
import sqlite3
import subprocess
import sys


def _safe(fn, default="unavailable"):
    try:
        return fn()
    except Exception as exc:  # pragma: no cover - environment probe
        return f"{default} ({type(exc).__name__}: {exc})"


def collect() -> dict:
    import claude_agent_sdk as sdk

    exports = sorted(n for n in dir(sdk) if not n.startswith("_"))

    option_fields = {
        f.name: str(f.type) for f in dataclasses.fields(sdk.ClaudeAgentOptions)
    }

    # Hook event names are parsed from the installed annotation rather than
    # copied from documentation: the docs list events this build may not accept.
    hooks_type = next(
        f.type for f in dataclasses.fields(sdk.ClaudeAgentOptions) if f.name == "hooks"
    )
    hook_events = sorted(set(re.findall(r"Literal\['(\w+)'\]", str(hooks_type))))

    signatures = {}
    for name in ("query", "tool", "create_sdk_mcp_server"):
        obj = getattr(sdk, name, None)
        if obj is not None:
            signatures[name] = _safe(lambda o=obj: str(inspect.signature(o)))

    client_methods = sorted(
        m for m in dir(sdk.ClaudeSDKClient) if not m.startswith("_")
    )

    result_fields = [f.name for f in dataclasses.fields(sdk.ResultMessage)]

    cli_version = "not found on PATH"
    if shutil.which("claude"):
        cli_version = _safe(
            lambda: subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=20
            ).stdout.strip()
        )

    # An `import textual` here is an availability probe, not a use: the value
    # comes from `md.version` below. Ruff flags it as unused (F401) and it is
    # not — deleting it would silently turn "NOT INSTALLED" into a crash.
    # `find_spec` states the intent directly and carries no side effect.
    if importlib.util.find_spec("textual") is not None:
        textual_version = _safe(lambda: md.version("textual"))
    else:
        textual_version = "NOT INSTALLED"

    git_version = "NOT ON PATH"
    git_path = shutil.which("git")
    if git_path:
        git_version = _safe(
            lambda: subprocess.run(
                ["git", "--version"], capture_output=True, text=True, timeout=20
            ).stdout.strip()
        )

    return {
        "sdk_version": _safe(lambda: md.version("claude-agent-sdk")),
        "sdk_path": sdk.__file__,
        "claude_cli": cli_version,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "git": git_version,
        "git_path": git_path or "NOT ON PATH",
        "textual": textual_version,
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "export_count": len(exports),
        "exports": exports,
        "option_field_count": len(option_fields),
        "option_fields": option_fields,
        "hook_events": hook_events,
        "signatures": signatures,
        "client_methods": client_methods,
        "result_message_fields": result_fields,
    }


def main() -> int:
    data = collect()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0

    print(f"claude-agent-sdk  {data['sdk_version']}")
    print(f"  path            {data['sdk_path']}")
    print(f"claude CLI        {data['claude_cli']}")
    print(f"python            {data['python']}")
    print(f"sqlite            {data['sqlite']}")
    print(f"git               {data['git']} ({data['git_path']})")
    print(f"textual           {data['textual']}")
    print(f"platform          {data['platform']}")
    print()
    print(f"exports           {data['export_count']}")
    print(f"option fields     {data['option_field_count']}")
    print(f"hook events       {', '.join(data['hook_events'])}")
    print(f"client methods    {', '.join(data['client_methods'])}")
    print()
    for name, sig in data["signatures"].items():
        print(f"{name}{sig}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
