# vendor/

Third-party MCP server implementations kept as Git submodules for reference and
tool-parity tracking.

## Unreal-MCP-Ghost

**Repository:** <https://github.com/CrispyW0nton/Unreal-MCP-Ghost>
**Branch:** `genspark_ai_developer`
**License:** MIT (fork of [chongdashu/unreal-mcp](https://github.com/chongdashu/unreal-mcp))

### Why this submodule exists

`mcp-unreal` and `Unreal-MCP-Ghost` solve the same problem — giving AI agents
programmatic control of the Unreal Engine 5 editor via the Model Context Protocol —
but they take fundamentally different architectural approaches.

`mcp-unreal` uses Unreal's **built-in** Remote Execution infrastructure (the Python
Script Plugin + the Remote Execution setting already shipped with every UE5 install).
No C++ compilation, no custom plugin, no extra listening port to manage.

`Unreal-MCP-Ghost` requires you to **compile and install a C++ editor plugin**
(`UnrealMCP`) into your project. That plugin starts a TCP server on
`localhost:55557` and exposes 119 custom C++ commands.  The Python MCP layer on top
wraps those into 311 specialized MCP tools.

The submodule is kept here so we can track what capabilities Unreal-MCP-Ghost
exposes and ensure `mcp-unreal` can match them through UE's native Python scripting
— without asking users to compile a plugin.

### Architecture comparison

|                        | **mcp-unreal**                                         | **Unreal-MCP-Ghost**                                  |
|------------------------|--------------------------------------------------------|-------------------------------------------------------|
| **Plugin required**    | No — uses UE's shipped Python Script Plugin            | Yes — custom C++ plugin compiled in VS 2022           |
| **Connection method**  | UE Remote Execution (UDP multicast `239.0.0.1:6766` discovery + direct TCP command) or optional HTTP bridge (`ue_server.py`, port 6800) | C++ plugin hosts TCP JSON server on `localhost:55557` |
| **Setup effort**       | Enable *Remote Execution* in Project Settings, run MCP server | Clone plugin source → copy to `Plugins/` → generate VS project files → compile → verify DLL |
| **Tool design**        | Single `execute-script` tool — sends arbitrary Python to UE's embedded interpreter | 311 specialized MCP tools + 119 raw C++ commands      |
| **Game-thread access** | HTTP bridge dispatches via Slate tick callback; native remote exec runs on game thread by default | All commands dispatched to game thread via `AsyncTask` |

### What we want from this reference

- **Tool parity list** — Unreal-MCP-Ghost's 311-tool catalog and its
  `knowledge_base/` docs are a useful roadmap for what capabilities AI agents
  need when working with UE5.
- **No plugin requirement** — `mcp-unreal`'s goal is to deliver those same
  capabilities through `execute-script` running UE Python, so users never have
  to touch Visual Studio or compile C++.
