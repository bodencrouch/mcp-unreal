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

POST /ghost_command
    Body: {"type": "ghost_command_name", "params": {...}}
    Returns: {"status": "success", "result": {...}} or
             {"status": "error", "error": "..."}
"""

from __future__ import annotations

import http.server
import io
import json
import queue
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Callable, Optional

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

# Game-thread dispatch: background threads post callables and the Slate tick
# callback runs them on the main game thread.
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
            callback, done_event, result_box = _dispatch_queue.get_nowait()
        except queue.Empty:
            break
        try:
            result_box.append(callback())
        except Exception:  # noqa: BLE001
            result_box.append({"success": False, "output": [], "error": traceback.format_exc()})
        finally:
            done_event.set()


def _dispatch_to_game_thread(callback: Callable[[], dict], timeout: float = 60.0) -> dict:
    """Post *callback* to the game thread and block until it finishes."""
    done = threading.Event()
    result_box: list[dict] = []
    _dispatch_queue.put((callback, done, result_box))
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
        elif self.path == "/ghost_command":
            self._handle_ghost_command()
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
            result = _dispatch_to_game_thread(lambda: _execute_direct(code, exec_mode))
        else:
            result = _execute_direct(code, exec_mode)
        self._send_json(result)

    def _handle_ghost_command(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            req: dict = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._send_json(_ghost_error(f"Invalid JSON body: {exc}"), 400)
            return

        command = str(req.get("type", "")).strip()
        params = req.get("params") or {}
        if not command:
            self._send_json(_ghost_error("Missing command type."), 400)
            return

        if _tick_handle is not None:
            result = _dispatch_to_game_thread(lambda: _execute_ghost_command(command, params))
        else:
            result = _execute_ghost_command(command, params)
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


def _ghost_success(result: Optional[dict] = None) -> dict:
    return {"status": "success", "result": result or {}}


def _ghost_error(message: str) -> dict:
    return {"status": "error", "error": message}


def _execute_ghost_command(command: str, params: dict) -> dict:
    handlers: dict[str, Callable[[dict], dict]] = {
        "get_actors_in_level": _ghost_get_actors_in_level,
        "find_actors_by_name": _ghost_find_actors_by_name,
        "spawn_actor": _ghost_spawn_actor,
        "delete_actor": _ghost_delete_actor,
        "set_actor_transform": _ghost_set_actor_transform,
        "get_actor_properties": _ghost_get_actor_properties,
        "set_actor_property": _ghost_set_actor_property,
        "spawn_blueprint_actor": _ghost_spawn_blueprint_actor,
        "create_blueprint": _ghost_create_blueprint,
        "compile_blueprint": _ghost_compile_blueprint,
        "set_blueprint_property": _ghost_set_blueprint_property,
        "set_blueprint_ai_controller": _ghost_set_blueprint_ai_controller,
        "focus_viewport": _ghost_focus_viewport,
        "take_screenshot": _ghost_take_screenshot,
    }

    if command == "exec_python":
        result = _execute_direct(
            str(params.get("code", "")),
            str(params.get("mode", "ExecuteStatement")),
        )
        output_lines = [entry.get("output", "") for entry in result.get("output", []) if entry.get("output")]
        if result.get("result") not in (None, ""):
            output_lines.append(f"Return: {result['result']}")
        inner = {
            "success": bool(result.get("success", False) and not result.get("error")),
            "output": "\n".join(output_lines).strip(),
            "result": result.get("result"),
        }
        if result.get("error"):
            inner["error"] = result["error"]
        return _ghost_success(inner)

    handler = handlers.get(command)
    if handler is None:
        return _ghost_error(
            f"Command '{command}' is not implemented by the HTTP Ghost bridge yet. "
            "Use the plugin backend on port 55557 for full Ghost coverage, or use exec_python where appropriate."
        )

    try:
        return handler(params or {})
    except Exception:  # noqa: BLE001
        return _ghost_error(traceback.format_exc())


def _get_all_level_actors() -> list[Any]:
    import unreal  # type: ignore

    try:
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        return list(subsystem.get_all_level_actors())
    except Exception:
        return list(unreal.EditorLevelLibrary.get_all_level_actors())


def _find_actor(name: str) -> Any:
    lowered = name.lower()
    for actor in _get_all_level_actors():
        if actor.get_name().lower() == lowered or actor.get_actor_label().lower() == lowered:
            return actor
    return None


def _serialize_actor(actor: Any) -> dict:
    location = actor.get_actor_location()
    rotation = actor.get_actor_rotation()
    scale = actor.get_actor_scale3d()
    return {
        "name": actor.get_actor_label(),
        "object_name": actor.get_name(),
        "type": actor.get_class().get_name(),
        "location": [float(location.x), float(location.y), float(location.z)],
        "rotation": [float(rotation.pitch), float(rotation.yaw), float(rotation.roll)],
        "scale": [float(scale.x), float(scale.y), float(scale.z)],
    }


def _vector(values: list[Any], default: tuple[float, float, float]) -> Any:
    import unreal  # type: ignore

    source = list(values or default)
    source.extend(list(default[len(source):]))
    return unreal.Vector(float(source[0]), float(source[1]), float(source[2]))


def _rotator(values: list[Any], default: tuple[float, float, float]) -> Any:
    import unreal  # type: ignore

    source = list(values or default)
    source.extend(list(default[len(source):]))
    return unreal.Rotator(float(source[0]), float(source[1]), float(source[2]))


def _resolve_class(name: str) -> Any:
    import unreal  # type: ignore

    candidate = getattr(unreal, name, None)
    if candidate is not None:
        return candidate

    search_paths = [
        f"/Script/Engine.{name}",
        f"/Script/CoreUObject.{name}",
        f"/Script/UMG.{name}",
        f"/Script/AIModule.{name}",
        f"/Script/NavigationSystem.{name}",
        f"/Script/GameplayTasks.{name}",
        f"/Script/EnhancedInput.{name}",
    ]
    for path in search_paths:
        loaded = unreal.load_class(None, path)
        if loaded is not None:
            return loaded
    return None


def _find_blueprint_asset(blueprint_name: str) -> Any:
    import unreal  # type: ignore

    common_paths = [
        f"/Game/Blueprints/{blueprint_name}",
        f"/Game/{blueprint_name}",
    ]
    for path in common_paths:
        asset = unreal.EditorAssetLibrary.load_asset(path)
        if asset is not None:
            return asset

    try:
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        for asset_data in registry.get_assets_by_class(unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")):
            if str(asset_data.asset_name) == blueprint_name:
                return asset_data.get_asset()
    except Exception:
        pass
    return None


def _coerce_property_value(value: Any) -> Any:
    return value


def _ghost_get_actors_in_level(params: dict) -> dict:
    actors = [_serialize_actor(actor) for actor in _get_all_level_actors()]
    return _ghost_success({"actors": actors})


def _ghost_find_actors_by_name(params: dict) -> dict:
    pattern = str(params.get("pattern", "")).lower()
    matches = []
    for actor in _get_all_level_actors():
        if pattern in actor.get_name().lower() or pattern in actor.get_actor_label().lower():
            matches.append(actor.get_actor_label())
    return _ghost_success({"actors": matches})


def _ghost_spawn_actor(params: dict) -> dict:
    import unreal  # type: ignore

    actor_class = _resolve_class(str(params.get("type", "")))
    if actor_class is None:
        return _ghost_error(f"Unknown actor class '{params.get('type', '')}'")

    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        actor_class,
        _vector(params.get("location", [0.0, 0.0, 0.0]), (0.0, 0.0, 0.0)),
        _rotator(params.get("rotation", [0.0, 0.0, 0.0]), (0.0, 0.0, 0.0)),
    )
    if actor is None:
        return _ghost_error("Failed to spawn actor")

    if params.get("name"):
        actor.set_actor_label(str(params["name"]))
    return _ghost_success(_serialize_actor(actor))


def _ghost_delete_actor(params: dict) -> dict:
    import unreal  # type: ignore

    actor = _find_actor(str(params.get("name", "")))
    if actor is None:
        return _ghost_error(f"Actor '{params.get('name', '')}' not found")
    unreal.EditorLevelLibrary.destroy_actor(actor)
    return _ghost_success({"success": True, "name": str(params.get("name", ""))})


def _ghost_set_actor_transform(params: dict) -> dict:
    actor = _find_actor(str(params.get("name", "")))
    if actor is None:
        return _ghost_error(f"Actor '{params.get('name', '')}' not found")
    if params.get("location") is not None:
        actor.set_actor_location(_vector(params.get("location", [0.0, 0.0, 0.0]), (0.0, 0.0, 0.0)))
    if params.get("rotation") is not None:
        actor.set_actor_rotation(_rotator(params.get("rotation", [0.0, 0.0, 0.0]), (0.0, 0.0, 0.0)))
    if params.get("scale") is not None:
        actor.set_actor_scale3d(_vector(params.get("scale", [1.0, 1.0, 1.0]), (1.0, 1.0, 1.0)))
    return _ghost_success(_serialize_actor(actor))


def _ghost_get_actor_properties(params: dict) -> dict:
    actor = _find_actor(str(params.get("name", "")))
    if actor is None:
        return _ghost_error(f"Actor '{params.get('name', '')}' not found")
    return _ghost_success(_serialize_actor(actor))


def _ghost_set_actor_property(params: dict) -> dict:
    actor = _find_actor(str(params.get("name", "")))
    if actor is None:
        return _ghost_error(f"Actor '{params.get('name', '')}' not found")
    actor.set_editor_property(str(params.get("property_name", "")), _coerce_property_value(params.get("property_value")))
    return _ghost_success({
        "success": True,
        "name": str(params.get("name", "")),
        "property_name": str(params.get("property_name", "")),
        "property_value": params.get("property_value"),
    })


def _ghost_spawn_blueprint_actor(params: dict) -> dict:
    import unreal  # type: ignore

    blueprint = _find_blueprint_asset(str(params.get("blueprint_name", "")))
    if blueprint is None:
        return _ghost_error(f"Blueprint '{params.get('blueprint_name', '')}' not found")
    actor_class = getattr(blueprint, "generated_class", None)
    if actor_class is None:
        return _ghost_error(f"Blueprint '{params.get('blueprint_name', '')}' has no generated class")

    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        actor_class,
        _vector(params.get("location", [0.0, 0.0, 0.0]), (0.0, 0.0, 0.0)),
        _rotator(params.get("rotation", [0.0, 0.0, 0.0]), (0.0, 0.0, 0.0)),
    )
    if actor is None:
        return _ghost_error("Failed to spawn Blueprint actor")

    if params.get("actor_name"):
        actor.set_actor_label(str(params["actor_name"]))
    return _ghost_success(_serialize_actor(actor))


def _ghost_create_blueprint(params: dict) -> dict:
    import unreal  # type: ignore

    parent_class = _resolve_class(str(params.get("parent_class", "")))
    if parent_class is None:
        return _ghost_error(f"Unknown parent class '{params.get('parent_class', '')}'")

    factory = unreal.BlueprintFactory()
    factory.set_editor_property("ParentClass", parent_class)
    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
    package_path = "/Game/Blueprints"
    blueprint = asset_tools.create_asset(str(params.get("name", "")), package_path, unreal.Blueprint, factory)
    if blueprint is None:
        return _ghost_error("Failed to create Blueprint asset")

    unreal.EditorAssetLibrary.save_loaded_asset(blueprint)
    return _ghost_success({
        "success": True,
        "name": str(params.get("name", "")),
        "path": f"{package_path}/{params.get('name', '')}",
    })


def _ghost_compile_blueprint(params: dict) -> dict:
    import unreal  # type: ignore

    blueprint = _find_blueprint_asset(str(params.get("blueprint_name", "")))
    if blueprint is None:
        return _ghost_error(f"Blueprint '{params.get('blueprint_name', '')}' not found")
    unreal.KismetEditorUtilities.compile_blueprint(blueprint)
    unreal.EditorAssetLibrary.save_loaded_asset(blueprint)
    return _ghost_success({
        "success": True,
        "blueprint_name": str(params.get("blueprint_name", "")),
        "compiled": True,
    })


def _ghost_set_blueprint_property(params: dict) -> dict:
    blueprint = _find_blueprint_asset(str(params.get("blueprint_name", "")))
    if blueprint is None:
        return _ghost_error(f"Blueprint '{params.get('blueprint_name', '')}' not found")
    generated_class = getattr(blueprint, "generated_class", None)
    if generated_class is None:
        return _ghost_error(f"Blueprint '{params.get('blueprint_name', '')}' has no generated class")
    default_object = generated_class.get_default_object()
    default_object.set_editor_property(
        str(params.get("property_name", "")),
        _coerce_property_value(params.get("property_value")),
    )
    return _ghost_success({
        "success": True,
        "blueprint_name": str(params.get("blueprint_name", "")),
        "property_name": str(params.get("property_name", "")),
        "property_value": params.get("property_value"),
    })


def _ghost_set_blueprint_ai_controller(params: dict) -> dict:
    blueprint = _find_blueprint_asset(str(params.get("blueprint_name", "")))
    if blueprint is None:
        return _ghost_error(f"Blueprint '{params.get('blueprint_name', '')}' not found")
    generated_class = getattr(blueprint, "generated_class", None)
    if generated_class is None:
        return _ghost_error(f"Blueprint '{params.get('blueprint_name', '')}' has no generated class")
    controller_class = _resolve_class(str(params.get("controller_class", "AIController")))
    if controller_class is None:
        return _ghost_error(f"Unknown controller class '{params.get('controller_class', 'AIController')}'")
    default_object = generated_class.get_default_object()
    default_object.set_editor_property("ai_controller_class", controller_class)
    return _ghost_success({
        "success": True,
        "blueprint_name": str(params.get("blueprint_name", "")),
        "ai_controller_class": str(params.get("controller_class", "AIController")),
    })


def _ghost_focus_viewport(params: dict) -> dict:
    import unreal  # type: ignore

    actor = _find_actor(str(params.get("name", "")))
    if actor is None:
        return _ghost_error(f"Actor '{params.get('name', '')}' not found")
    try:
        unreal.EditorLevelLibrary.set_selected_level_actors([actor])
    except Exception:
        pass
    return _ghost_success({"success": True, "focused_actor": actor.get_actor_label()})


def _ghost_take_screenshot(params: dict) -> dict:
    import unreal  # type: ignore

    filename = str(params.get("filename", "ghost_screenshot"))
    resolution = params.get("resolution", [1920, 1080])
    width = int(resolution[0]) if len(resolution) > 0 else 1920
    height = int(resolution[1]) if len(resolution) > 1 else 1080
    if hasattr(unreal, "AutomationLibrary"):
        unreal.AutomationLibrary.take_high_res_screenshot(width, height, filename)
        return _ghost_success({"success": True, "filename": filename})
    return _ghost_error("AutomationLibrary is not available for screenshots in this editor session")


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
