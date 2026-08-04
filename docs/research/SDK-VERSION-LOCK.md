# SDK-VERSION-LOCK

Requirement: `SDK-001` — the installed Agent SDK API must be inventoried and pinned.

Every version below was read from the running environment, not from documentation.
Reproduce with `tools/sdk-inventory.py`.

## Which environment to verify in

Build the declared environment first. The SDK is **not** installed in every
interpreter on a developer machine, and an unrelated ambient install will
report different numbers:

```
python3 -m venv /tmp/dreamy-agent
PIP_USER=0 /tmp/dreamy-agent/bin/pip install --require-hashes -r config/requirements-agent.lock
/tmp/dreamy-agent/bin/python tools/sdk-inventory.py
```

`PIP_USER=0` must sit on the `pip` invocation. Placing it before `venv` does not
carry into the later `pip` call and fails with `Can not perform a '--user'
install. User site-packages are not visible in this virtualenv.`

This is not hypothetical. Checking the ambient `python3` on the development
machine reports `claude-agent-sdk 0.1.68`, 136 exports, and 42 option fields —
an older unrelated install. Reading those numbers as authoritative produced a
false "the lock file is stale" conclusion. The lock is a *declaration*; the
global interpreter is not the project environment. Verify against the
hash-locked install, and record which interpreter produced any figure.

## Locked versions

| Component | Version | How observed |
|---|---|---|
| `claude-agent-sdk` | `0.2.128` | `importlib.metadata.version("claude-agent-sdk")` |
| Claude CLI | `2.1.220 (Claude Code)` | `claude --version` |
| Python | `3.11.6` | `sys.version` |
| SQLite | `3.42.0` | `sqlite3.sqlite_version` |
| Git | `2.55.0` at `/opt/homebrew/bin/git` (required; `cli.py` exits 127 without it) | `git --version` |
| Textual (installed) | **NOT INSTALLED** — no version observed | `import textual` → `ModuleNotFoundError` |
| Textual (pin / test target) | `8.2.8` — declared only, **never executed here** | `tui` extra in `pyproject.toml` |
| Platform | `Darwin 27.0.0 arm64` | `platform.system/release/machine` |

The SDK pin is expressed as `claude-agent-sdk==0.2.128` in the `agent` extra and
resolved with transitive hashes into `config/requirements-agent.lock`.

## Consequence of the Textual gap

`dreamy.tui.*` is unimportable in this environment. G11 (TUI, six real-data
views) is therefore **recorded blocked**, not claimed. The `tui` extra pins
`textual==8.2.8`, which is the version G11 must be validated against once
installed. No TUI behaviour has been observed in this environment and none is
asserted anywhere in the acceptance evidence.

## Installed API surface

- `claude_agent_sdk` exports **145** public symbols.
- `ClaudeAgentOptions` is a dataclass with **45** fields.
- `ClaudeSDKClient` exposes 15 public methods:
  `connect`, `disconnect`, `get_context_usage`, `get_mcp_status`,
  `get_server_info`, `interrupt`, `query`, `receive_messages`,
  `receive_response`, `reconnect_mcp_server`, `rewind_files`, `set_model`,
  `set_permission_mode`, `stop_task`, `toggle_mcp_server`.

### Observed signatures

```python
query(*, prompt: str | AsyncIterable[dict[str, Any]],
      options: ClaudeAgentOptions | None = None,
      transport: Transport | None = None) -> AsyncIterator[...]

tool(name: str, description: str, input_schema: type | dict[str, Any],
     annotations: ToolAnnotations | None = None) -> Callable[..., SdkMcpTool[Any]]

create_sdk_mcp_server(name: str, version: str = '1.0.0',
                      tools: list[SdkMcpTool[Any]] | None = None) -> McpSdkServerConfig

ClaudeSDKClient.rewind_files(self, user_message_id: str) -> None
```

### Hook events actually accepted by `ClaudeAgentOptions.hooks`

Parsed from the installed type, not from documentation:

`Notification`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`,
`PreCompact`, `PreToolUse`, `Stop`, `SubagentStart`, `SubagentStop`,
`UserPromptSubmit`

This list is authoritative for `OBS-004`. Any hook name outside it must not
appear in Dreamy configuration; the attachment's wishlist mentions events such
as session-start/end, cost updates, and agent completion that have **no direct
hook event** in `0.2.128` and must be derived from message stream state instead.

### `ResultMessage` fields relevant to `SDK-014` cost accounting

`subtype`, `duration_ms`, `duration_api_ms`, `is_error`, `num_turns`,
`session_id`, `stop_reason`, `total_cost_usd`, `usage`, `result`,
`structured_output`, `model_usage`, `permission_denials`, `deferred_tool_use`,
`errors`, `api_error_status`, `uuid`, `terminal_reason`

`total_cost_usd` and `model_usage` are the documented cost sources; `SDK-014`
must read these rather than estimating from token counts.

## Change policy

Bumping any version here requires, in the same pull request:

1. re-running `tools/sdk-inventory.py` and committing the refreshed inventory,
2. re-running the `tests/sdk_conformance/` suite against the new version,
3. regenerating `config/requirements-agent.lock` via `tools/lock-deps.sh`,
4. updating `docs/research/SDK-CAPABILITY-MATRIX.md` for any capability whose
   signature, disposition, or failure behaviour changed.

An SDK bump without fresh conformance evidence is a G3 failure.
