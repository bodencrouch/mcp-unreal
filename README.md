# mcp-unreal

MCP stdio server that executes Python scripts inside a running **Unreal Engine 5** editor.
Exposes a single `execute-script` MCP tool backed by UE's built-in Python Remote Execution protocol.

## Architecture

### Connection backends

| Backend | Description | When to use |
|---------|-------------|-------------|
| **Native Remote Execution** (default) | UDP multicast discovery + direct TCP command socket. Ships with the UE Python Script Plugin — no extra plugins needed. | Local UE editor on the same machine or LAN. |
| **McpAutomationBridge** | HTTP REST calls to the [`McpAutomationBridge`](https://github.com/ChiR24/Unreal_mcp) C++ plugin. | UE editor on a remote machine with the plugin installed. |

### Key files

| File | Purpose |
|------|---------|
| `src/mcp_unreal/cli.py` | CLI entry point; parses `--host`, `--port`, `--transport`, etc. |
| `src/mcp_unreal/server.py` | MCP server; defines the `execute-script` tool and result formatter |
| `src/mcp_unreal/ue_remote.py` | UE remote execution client: discovery, framing, both backends |

## Build & run

```bash
uv sync
uv run mcp-unreal                                  # stdio, UDP discovery
uv run mcp-unreal --host 127.0.0.1 --ue-port 6969 # direct TCP
uv run mcp-unreal --transport http --port 8080     # HTTP/SSE transport
uv run mcp-unreal --bridge-url http://localhost:8091  # McpAutomationBridge
```

## Unreal Engine setup

1. Enable the **Python Script Plugin** in your UE project (Edit → Plugins → Python).
2. In **Project Settings → Plugins → Python**, enable **Remote Execution**.
3. Run the UE editor (Play-In-Editor is fine).
4. Start `mcp-unreal` on the same machine (or specify `--host` for remote).

## VS Code / mcp.json integration

```jsonc
{
  "servers": {
    "mcp-unreal": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--with-editable", "C:/GitHub/mcp-unreal", "mcp-unreal"]
    }
  }
}
```

## Tool reference

### `execute-script`

Executes arbitrary Python code in the UE scripting environment.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `code` | string | **required** | Python source code to run |
| `exec_mode` | string | `ExecuteStatement` | `ExecuteStatement` / `EvaluateStatement` / `ExecuteFile` |
| `unattended` | boolean | `true` | Suppress interactive UE dialogs |

**Useful `unreal` module entry points:**
- `unreal.EditorAssetLibrary` – asset CRUD operations
- `unreal.EditorLevelLibrary` – actor spawn/query/modify
- `unreal.SystemLibrary` – general utilities
- `unreal.log()` / `unreal.log_warning()` / `unreal.log_error()` – structured output

**API docs:** https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api
