"""
ue_server.py — Unreal Engine-side Python execution HTTP server for mcp-unreal.

Drop this file anywhere on the UE host machine and run it once from UE's
Python console (Output Log → Python tab):

    import sys, importlib
    sys.path.insert(0, r"C:/path/to/mcp-unreal/plugins/ue_python_server")
    import ue_server; ue_server.start()

Or paste the entire file content directly into the Python console.

What this does
--------------
Starts a lightweight HTTP server in a daemon background thread.  The server
accepts POST /execute_python requests from the mcp-unreal MCP process, executes
the Python code here in UE's embedded Python interpreter (which has full access
to the `unreal` module), and returns captured output + return value as JSON.

This is the correct way to expose an arbitrary Python REPL over the network for
UE5, since the McpAutomationBridge C++ plugin exposes only structured editor
operations and does not have a /execute_python endpoint.

Endpoints
---------
POST /execute_python
    Body: {"command": "...", "exec_mode": "ExecuteStatement|EvaluateStatement|ExecuteFile",
           "unattended": true}
    Returns: {"success": bool, "output": [{"type": "info|warning|error", "output": "..."}],
              "result": "<repr of return value>"}

GET /ping
    Returns: {"status": "ok", "engine": "<UE version string>"}

POST /ping
    Same as GET /ping.
"""

from __future__ import annotations

import http.server
import io
import json
import queue
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6800

_server: Optional[http.server.HTTPServer] = None
_thread: Optional[threading.Thread] = None

# Persistent execution namespace — imports accumulate across calls
_exec_ns: dict[str, Any] = {"__name__": "__main__", "__builtins__": __builtins__}
_exec_ns_lock = threading.Lock()

# Game-thread dispatch: background threads post (code, exec_mode, event, result_box)
# and the Slate tick callback picks them up on the main game thread.
_dispatch_queue: queue.Queue = queue.Queue()
_tick_handle: Any = None


def start(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """
    Start the Python execution HTTP server in a daemon background thread.

    Call this once from UE's Python console.  The server keeps running for
    the lifetime of the UE editor process.  Any mcp-unreal MCP server instance
    pointed at --bridge-url http://<host>:<port> will be able to execute
    Python scripts via this server.
    """
    global _server, _thread

    if _server is not None:
        _ue_log(f"[mcp-unreal] Server already running at http://{host}:{port}")
        return

    global _tick_handle

    try:
        import unreal  # type: ignore
        _exec_ns["unreal"] = unreal
        # Register Slate post-tick callback so dispatched code runs on the game thread
        _tick_handle = unreal.register_slate_post_tick_callback(_game_thread_tick) # pyright: ignore[reportAttributeAccessIssue]
        _ue_log("[mcp-unreal] Game-thread tick callback registered.")
    except ImportError:
        pass

    _server = http.server.HTTPServer((host, port), _Handler)
    _thread = threading.Thread(
        target=_server.serve_forever,
        daemon=True,
        name="mcp-unreal-server",
    )
    _thread.start()
    _ue_log(f"[mcp-unreal] Python execution server started → http://{host}:{port}")
    _ue_log("[mcp-unreal] Waiting for connections from mcp-unreal MCP server...")


def _game_thread_tick(delta: float) -> None:
    """Called every Slate tick on the game thread. Drains the dispatch queue."""
    while True:
        try:
            code, exec_mode, done_event, result_box = _dispatch_queue.get_nowait()
        except queue.Empty:
            break
        try:
            result_box.append(_execute_direct(code, exec_mode))
        except Exception:  # noqa: BLE001
            result_box.append({"success": False, "output": [], "error": traceback.format_exc()})
        finally:
            done_event.set()


def _dispatch_to_game_thread(code: str, exec_mode: str, timeout: float = 60.0) -> dict:
    """Post *code* to the game thread and block until it finishes."""
    done = threading.Event()
    result_box: list[dict] = []
    _dispatch_queue.put((code, exec_mode, done, result_box))
    if not done.wait(timeout):
        return {"success": False, "output": [], "error": f"Game-thread execution timed out after {timeout}s"}
    return result_box[0] if result_box else {"success": False, "output": [], "error": "No result"}


def stop() -> None:
    """Shut down the HTTP server."""
    global _server, _thread, _tick_handle
    if _tick_handle is not None:
        try:
            import unreal  # type: ignore
            unreal.unregister_slate_post_tick_callback(_tick_handle) # pyright: ignore[reportAttributeAccessIssue]
        except Exception:  # noqa: BLE001
            pass
        _tick_handle = None
    if _server is not None:
        _server.shutdown()
        _server = None
        _thread = None
        _ue_log("[mcp-unreal] Server stopped.")


def is_running() -> bool:
    return _server is not None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # suppress default per-request access log noise

    def do_GET(self) -> None:
        if self.path in ("/ping", "/"):
            self._send_json({"status": "ok", "engine": _engine_version()})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/execute_python":
            self._handle_execute()
        elif self.path == "/ping":
            self._send_json({"status": "ok", "engine": _engine_version()})
        else:
            self.send_error(404)

    # ------------------------------------------------------------------

    def _handle_execute(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            req: dict = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._send_json({"success": False, "error": f"Invalid JSON body: {exc}"}, 400)
            return

        code: str = req.get("command", "")
        exec_mode: str = req.get("exec_mode", "ExecuteStatement")

        if not code.strip():
            self._send_json({"success": False, "error": "Empty command."}, 400)
            return

        # If a game-thread tick is registered, dispatch there for full unreal API access.
        # Fall back to direct (background-thread) execution when running outside UE.
        if _tick_handle is not None:
            result = _dispatch_to_game_thread(code, exec_mode)
        else:
            result = _execute_direct(code, exec_mode)
        self._send_json(result)

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Python execution
# ---------------------------------------------------------------------------

def _execute_direct(code: str, exec_mode: str) -> dict:
    """
    Run *code* inside UE's Python environment.

    stdout/stderr are captured via redirect; unreal.log / unreal.log_warning /
    unreal.log_error are monkey-patched for the duration so their output is also
    captured in the result.

    Must be called from the game thread when UE APIs are used.

    Returns a dict with keys: success, output, result (optional).
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    captured_ue_logs: list[dict] = []

    success = False
    return_value: Any = None

    # ---- monkey-patch unreal.log* so output is captured ----
    unreal_patches: list[tuple[Any, str, Any]] = []
    try:
        import unreal as _unreal_mod  # type: ignore

        def _make_log_capture(level: str, original):
            def _cap(msg, *a, **kw):
                captured_ue_logs.append({"type": level, "output": str(msg)})
                try:
                    original(msg, *a, **kw)
                except Exception:
                    pass
            return _cap

        for _attr, _level in (("log", "info"), ("log_warning", "warning"), ("log_error", "error")):
            _orig = getattr(_unreal_mod, _attr, None)
            if _orig is not None:
                unreal_patches.append((_unreal_mod, _attr, _orig))
                setattr(_unreal_mod, _attr, _make_log_capture(_level, _orig))
    except ImportError:
        pass

    # ---- execute ----
    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            with _exec_ns_lock:
                if exec_mode == "EvaluateStatement":
                    return_value = eval(  # noqa: S307
                        compile(code, "<mcp-unreal:eval>", "eval"),
                        _exec_ns,
                    )
                    success = True
                elif exec_mode == "ExecuteFile":
                    with open(code, encoding="utf-8") as fh:
                        src = fh.read()
                    exec(  # noqa: S102
                        compile(src, code, "exec"),
                        _exec_ns,
                    )
                    success = True
                else:  # ExecuteStatement (default)
                    exec(  # noqa: S102
                        compile(code, "<mcp-unreal>", "exec"),
                        _exec_ns,
                    )
                    success = True
    except Exception:  # noqa: BLE001
        stderr_buf.write(traceback.format_exc())
        success = False
    finally:
        # restore unreal.log* originals
        for mod, attr, original in unreal_patches:
            setattr(mod, attr, original)

    # ---- build output list ----
    output: list[dict] = []

    stdout_text = stdout_buf.getvalue()
    if stdout_text:
        for line in stdout_text.rstrip("\n").splitlines():
            output.append({"type": "info", "output": line})

    output.extend(captured_ue_logs)

    stderr_text = stderr_buf.getvalue()
    if stderr_text:
        for line in stderr_text.rstrip("\n").splitlines():
            output.append({"type": "error", "output": line})

    result: dict = {"success": success, "output": output}
    if return_value is not None:
        result["result"] = repr(return_value)

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ue_log(msg: str) -> None:
    """Print via unreal.log if available, else print()."""
    try:
        import unreal  # type: ignore
        unreal.log(msg)
    except Exception:  # noqa: BLE001
        print(msg)


def _engine_version() -> str:
    try:
        import unreal  # type: ignore
        return str(unreal.SystemLibrary.get_engine_version())
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# Convenience: auto-start when this file is exec'd directly in UE console
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    start()
