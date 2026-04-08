"""
ue_exec.py — execute arbitrary Python inside a running UE5 editor.

Usage (exactly like `python -c`):
    uv run python ue_exec.py "print('hello from UE5')"
    uv run python ue_exec.py "import unreal; unreal.log('hi')"

The code string is forwarded to ue_server.py running inside UE via HTTP.
Output captured from stdout/stderr/unreal.log is printed to this terminal.
"""

import sys
import httpx

BRIDGE_URL = "http://127.0.0.1:6800"

if len(sys.argv) < 2:
    print("Usage: uv run python ue_exec.py \"<python code>\"", file=sys.stderr)
    sys.exit(1)

code = sys.argv[1]

resp = httpx.post(
    f"{BRIDGE_URL}/execute_python",
    json={"command": code, "exec_mode": "ExecuteStatement", "unattended": True},
    timeout=30.0,
)
data = resp.json()

for entry in data.get("output", []):
    t = entry.get("type", "info")
    line = entry.get("output", "")
    if t == "error":
        print(f"[ERROR] {line}", file=sys.stderr)
    elif t == "warning":
        print(f"[WARN]  {line}")
    else:
        print(line)

if not data.get("success"):
    sys.exit(1)
