"""
ue_server_autostart.py — UE5 startup script for mcp-unreal.

This script is referenced in DefaultEngine.ini under:
    [/Script/PythonScriptPlugin.PythonScriptPluginSettings]
    +StartupScripts=C:/GitHub/mcp-unreal/plugins/ue_python_server/ue_server_autostart.py

UE will execute it automatically whenever the editor loads, starting the
mcp-unreal HTTP server on http://127.0.0.1:6800.

The MCP server (run separately) should then be invoked with:
    uv run mcp-unreal --bridge-url http://127.0.0.1:6800
"""

import sys as _sys
import os as _os

_plugin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _plugin_dir not in _sys.path:
    _sys.path.insert(0, _plugin_dir)

import ue_server as _ue_server  # noqa: E402

if not _ue_server.is_running():
    _ue_server.start()
