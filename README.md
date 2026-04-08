# mcp-unreal

MCP stdio server that executes Python scripts inside a running **Unreal Engine 5** editor.
Exposes a single `execute-script` MCP tool backed by either the built-in Python Remote Execution protocol or the `ue_server.py` HTTP bridge included in this repo.

## Architecture

### Connection backends

| Backend | Description | When to use |
|---------|-------------|-------------|
| **Native Remote Execution** (default) | UDP multicast discovery + direct TCP command socket. Ships with the UE Python Script Plugin — no extra plugins needed. | Local UE editor on the same machine or LAN. |
| **ue_server.py HTTP bridge** (`--bridge-url`) | Lightweight HTTP server that runs inside UE's embedded Python and dispatches all execution to the main game thread via a Slate tick callback. Required for game-thread APIs (`EditorLevelLibrary`, `SystemLibrary`, etc.). | Recommended for all local UE workflows. |

### Key files

| File | Purpose |
|------|---------|
| `src/mcp_unreal/cli.py` | CLI entry point; parses `--host`, `--port`, `--transport`, etc. All options settable via env vars (see below). |
| `src/mcp_unreal/server.py` | MCP server; defines the `execute-script` tool and result formatter |
| `src/mcp_unreal/ue_remote.py` | UE remote execution client: discovery, framing, both backends |
| `plugins/ue_python_server/ue_server.py` | HTTP bridge server run inside UE's Python runtime |
| `plugins/ue_python_server/ue_server_autostart.py` | UE startup script — auto-starts the bridge when UE opens |
| `ue_exec.py` | Convenience CLI: `python ue_exec.py "<code>"` like `python -c` |

## Installation

Install directly from GitHub — no clone required:

```bash
# Run once (ephemeral)
uvx --from "git+https://github.com/bodencrouch/mcp-unreal" mcp-unreal --help

# Or install persistently
uv tool install "git+https://github.com/bodencrouch/mcp-unreal"
mcp-unreal --help
```

For local development:

```bash
git clone https://github.com/bodencrouch/mcp-unreal
cd mcp-unreal
uv sync
uv run mcp-unreal --help
```

## Unreal Engine setup

### 1. Enable the Python Script Plugin

Edit → Plugins → search **Python** → enable **Python Script Plugin** → restart.

### 2. Enable Remote Execution

Project Settings → Plugins → Python → enable **Remote Execution**.

### 3. Auto-start the HTTP bridge (recommended)

Add to `Config/DefaultEngine.ini`:

```ini
[/Script/PythonScriptPlugin.PythonScriptPluginSettings]
bRemoteExecution=True
+StartupScripts=C:/GitHub/mcp-unreal/plugins/ue_python_server/ue_server_autostart.py
```

The bridge starts automatically on UE boot and listens on `http://127.0.0.1:6800`.

## Usage

### With the HTTP bridge (recommended)

```bash
# Via env var — no flags needed
UE_BRIDGE_URL=http://127.0.0.1:6800 uvx --from "git+https://github.com/bodencrouch/mcp-unreal" mcp-unreal

# Or with explicit flag
uvx --from "git+https://github.com/bodencrouch/mcp-unreal" mcp-unreal --bridge-url http://127.0.0.1:6800
```

### With native UDP discovery

```bash
uvx --from "git+https://github.com/bodencrouch/mcp-unreal" mcp-unreal
uvx --from "git+https://github.com/bodencrouch/mcp-unreal" mcp-unreal --host 127.0.0.1 --ue-port 6969
```

### Quick script runner (`ue_exec.py`)

```bash
# Exactly like python -c, but runs inside UE
uv run python ue_exec.py "import unreal; print(unreal.SystemLibrary.get_engine_version())"
uv run python ue_exec.py "actors = list(unreal.EditorLevelLibrary.get_all_level_actors()); print(len(actors))"
```

## Environment variables

All CLI options can be set via environment variables. **Env vars take priority over defaults; CLI flags take priority over env vars.**

| Env var | CLI flag | Default | Description |
|---------|----------|---------|-------------|
| `UE_BRIDGE_URL` | `--bridge-url` | *(none)* | HTTP bridge URL, e.g. `http://127.0.0.1:6800` |
| `UE_HOST` | `--host` | *(none)* | UE host for native remote execution |
| `UE_PORT` | `--ue-port` | *(none)* | UE TCP command port |
| `UE_COMMAND_TIMEOUT` | `--command-timeout` | `60` | Seconds to wait for script result |
| `UE_DISCOVERY_TIMEOUT` | `--discovery-timeout` | `5` | UDP discovery timeout (seconds) |
| `MCP_TRANSPORT` | `--transport` | `stdio` | `stdio` / `http` / `sse` |
| `MCP_PORT` | `--port` | `8080` | Port for HTTP/SSE transport |
| `MCP_BIND` | `--bind` | `127.0.0.1` | Bind address for HTTP/SSE transport |
| `MCP_LOG_LEVEL` | `--log-level` | `WARNING` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Cursor / VS Code mcp.json

### Cursor (`.cursor/mcp.json`)

```jsonc
{
  "mcpServers": {
    "mcp-unreal": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/bodencrouch/mcp-unreal",
        "mcp-unreal"
      ],
      "env": {
        "UE_BRIDGE_URL": "http://127.0.0.1:6800",
        "UE_COMMAND_TIMEOUT": "60",
        "MCP_LOG_LEVEL": "WARNING"
      }
    }
  }
}
```

### VS Code (`.vscode/mcp.json`)

```jsonc
{
  "servers": {
    "mcp-unreal": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/bodencrouch/mcp-unreal",
        "mcp-unreal"
      ],
      "env": {
        "UE_BRIDGE_URL": "http://127.0.0.1:6800"
      }
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
