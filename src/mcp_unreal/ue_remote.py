"""
Unreal Engine 5 Python Remote Execution client.

Implements the UE Python Remote Execution protocol shipped with the
PythonScriptPlugin (Engine/Plugins/Experimental/PythonScriptPlugin).

Two backends are supported:
  - native:  UDP multicast discovery + direct TCP command socket (built-in UE feature)
  - bridge:  HTTP REST calls to a running McpAutomationBridge plugin
              (https://github.com/ChiR24/Unreal_mcp)

Protocol overview (native):
  Discovery: multicast UDP on 239.0.0.1:6766
    → send "open_connection" message → UE replies with its TCP command port
  Commands:  TCP socket to UE command listener
    → 4-byte big-endian length prefix + UTF-8 JSON payload
    → UE replies with exec_result containing output + return value
"""

from __future__ import annotations

import json
import socket
import struct
import time
import uuid
import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants (must match UE PythonScriptPlugin/Source/PythonScriptPlugin)
# ---------------------------------------------------------------------------
_MAGIC = "ureremotexec"
_VERSION = 1

_MCAST_GROUP = "239.0.0.1"
_MCAST_PORT = 6766
_MCAST_TTL = 1

_DISCOVERY_TIMEOUT_S = 5.0
_COMMAND_TIMEOUT_S = 60.0         # scripts may take a while


class ExecMode:
    EXECUTE_FILE = "ExecuteFile"
    EXECUTE_STATEMENT = "ExecuteStatement"
    EVALUATE_STATEMENT = "EvaluateStatement"


# ---------------------------------------------------------------------------
# Low-level framing helpers
# ---------------------------------------------------------------------------

def _encode_msg(payload: dict) -> bytes:
    """Encode a JSON dict to a length-prefixed wire message."""
    data = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(data)) + data


def _read_msg(sock: socket.socket) -> dict:
    """Read one length-prefixed JSON message from *sock*."""
    raw_len = _recv_exactly(sock, 4)
    length = struct.unpack(">I", raw_len)[0]
    raw_data = _recv_exactly(sock, length)
    return json.loads(raw_data.decode("utf-8"))


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Remote execution connection closed unexpectedly")
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _make_msg(
    msg_type: str,
    data: dict,
    source_id: str,
    dest_id: Optional[str] = None,
) -> dict:
    return {
        "version": _VERSION,
        "magic": _MAGIC,
        "source": source_id,
        "dest": dest_id,
        "type": msg_type,
        "data": data,
    }


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ExecResult:
    success: bool
    return_value: Any            # repr() string from UE, or None
    output: list[dict]           # [{"type": "info"|"warning"|"error", "output": "..."}]
    error: Optional[str] = None  # set on transport-level error

    @property
    def stdout_lines(self) -> list[str]:
        return [
            entry["output"]
            for entry in self.output
            if entry.get("type") == "info"
        ]

    @property
    def warning_lines(self) -> list[str]:
        return [
            entry["output"]
            for entry in self.output
            if entry.get("type") == "warning"
        ]

    @property
    def error_lines(self) -> list[str]:
        return [
            entry["output"]
            for entry in self.output
            if entry.get("type") == "error"
        ]


# ---------------------------------------------------------------------------
# Native backend (UDP multicast discovery + TCP commands)
# ---------------------------------------------------------------------------

class RemoteExecutionClient:
    """
    Connects to a running UE5 editor via its Python Remote Execution protocol.

    Usage::

        client = RemoteExecutionClient(host="127.0.0.1")
        with client.connect() as conn:
            result = conn.exec("import unreal; print(unreal.Engine.game_engine_version())")
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        mcast_group: str = _MCAST_GROUP,
        mcast_port: int = _MCAST_PORT,
        discovery_timeout: float = _DISCOVERY_TIMEOUT_S,
        command_timeout: float = _COMMAND_TIMEOUT_S,
    ) -> None:
        """
        Parameters
        ----------
        host:
            If given, skip UDP discovery and connect directly to this TCP host.
        port:
            TCP command port (required when *host* is given; ignored if
            host is None because the port is obtained from discovery).
        mcast_group:
            Multicast group used by UE Remote Execution (default 239.0.0.1).
        mcast_port:
            Multicast port (default 6766).
        discovery_timeout:
            Seconds to wait for a UE node to respond during discovery.
        command_timeout:
            Seconds to wait for a script result after sending the exec request.
        """
        self.host = host
        self.port = port
        self.mcast_group = mcast_group
        self.mcast_port = mcast_port
        self.discovery_timeout = discovery_timeout
        self.command_timeout = command_timeout
        self._node_id = str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        code: str,
        exec_mode: str = ExecMode.EXECUTE_STATEMENT,
        unattended: bool = True,
    ) -> ExecResult:
        """
        Execute *code* in the UE Python scripting environment and return the result.

        Parameters
        ----------
        code:
            Python source code string to execute.
        exec_mode:
            One of ExecMode.EXECUTE_STATEMENT / EVALUATE_STATEMENT / EXECUTE_FILE.
        unattended:
            When True UE suppresses interactive dialogs (recommended).
        """
        host, port, ue_node_id = self._resolve_endpoint()
        return self._send_command(host, port, ue_node_id, code, exec_mode, unattended)

    # ------------------------------------------------------------------
    # Endpoint resolution
    # ------------------------------------------------------------------

    def _resolve_endpoint(self) -> tuple[str, int, Optional[str]]:
        """Return (host, port, ue_node_id).  ue_node_id may be None for direct mode."""
        if self.host is not None and self.port is not None:
            log.debug("Direct connection to %s:%d", self.host, self.port)
            return self.host, self.port, None

        if self.host is not None and self.port is None:
            raise ValueError(
                "--port is required when --host is specified without UDP discovery"
            )

        return self._discover_node()

    def _discover_node(self) -> tuple[str, int, str]:
        """Broadcast for UE nodes; return (host, tcp_port, ue_node_id)."""
        log.debug(
            "Discovering UE nodes on %s:%d (timeout %.1fs)",
            self.mcast_group,
            self.mcast_port,
            self.discovery_timeout,
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, _MCAST_TTL)
            sock.settimeout(self.discovery_timeout)

            # Listen for multicast replies on any local interface
            sock.bind(("", self.mcast_port))
            mreq = struct.pack(
                "4sL",
                socket.inet_aton(self.mcast_group),
                socket.INADDR_ANY,
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

            # Broadcast an open_connection request
            msg = _make_msg(
                "open_connection",
                {
                    "command_ip": "0.0.0.0",
                    "command_port": 0,
                },
                source_id=self._node_id,
            )
            raw = json.dumps(msg).encode("utf-8")
            sock.sendto(raw, (self.mcast_group, self.mcast_port))
            log.debug("Sent open_connection broadcast")

            # Wait for a "pong" / "node" reply
            deadline = time.monotonic() + self.discovery_timeout
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(65536)
                    reply = json.loads(data.decode("utf-8"))
                    log.debug("Discovery reply from %s: type=%s", addr, reply.get("type"))

                    if reply.get("magic") != _MAGIC:
                        continue
                    if reply.get("type") not in ("pong", "node", "open_connection"):
                        continue
                    if reply.get("source") == self._node_id:
                        continue  # own echo

                    rdata = reply.get("data", {})
                    command_port = rdata.get("command_port") or rdata.get("port")
                    command_host = rdata.get("command_ip") or addr[0]
                    ue_node_id = reply.get("source") or str(uuid.uuid4())

                    if command_port:
                        log.debug(
                            "Found UE node %s at %s:%d",
                            ue_node_id,
                            command_host,
                            command_port,
                        )
                        return command_host, int(command_port), ue_node_id

                except socket.timeout:
                    break
                except json.JSONDecodeError:
                    continue
        finally:
            sock.close()

        raise ConnectionError(
            "No Unreal Engine node found on the network. "
            "Ensure the UE editor is running with the Python Script Plugin and "
            "Remote Python Execution enabled in Project Settings."
        )

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def _send_command(
        self,
        host: str,
        port: int,
        ue_node_id: Optional[str],
        code: str,
        exec_mode: str,
        unattended: bool,
    ) -> ExecResult:
        """Open a TCP connection to UE, send *code*, and return the result."""
        log.debug("Connecting to command endpoint %s:%d", host, port)

        try:
            sock = socket.create_connection((host, port), timeout=self.command_timeout)
        except OSError as exc:
            return ExecResult(
                success=False,
                return_value=None,
                output=[],
                error=f"Cannot connect to UE remote execution at {host}:{port}: {exc}",
            )

        try:
            sock.settimeout(self.command_timeout)

            exec_msg = _make_msg(
                "exec",
                {
                    "command": code,
                    "exec_mode": exec_mode,
                    "unattended": unattended,
                },
                source_id=self._node_id,
                dest_id=ue_node_id,
            )
            sock.sendall(_encode_msg(exec_msg))
            log.debug("Sent exec message (%d bytes of code)", len(code))

            reply = _read_msg(sock)
            log.debug("Received reply type=%s", reply.get("type"))

            if reply.get("type") != "exec_result":
                return ExecResult(
                    success=False,
                    return_value=None,
                    output=[],
                    error=f"Unexpected reply type: {reply.get('type')}",
                )

            rdata = reply.get("data", {})
            return ExecResult(
                success=bool(rdata.get("success", False)),
                return_value=rdata.get("result"),
                output=rdata.get("output", []),
            )

        except Exception as exc:  # noqa: BLE001
            return ExecResult(
                success=False,
                return_value=None,
                output=[],
                error=f"Error during remote execution: {exc}",
            )
        finally:
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# HTTP bridge backend — targets plugins/ue_python_server/ue_server.py
# ---------------------------------------------------------------------------

class AutomationBridgeClient:
    """
    Execute Python via the ue_server.py HTTP server running inside UE's Python
    environment.

    ``ue_server.py`` (shipped in plugins/ue_python_server/) must be started once
    inside the Unreal Editor Python console before this client can be used::

        import sys
        sys.path.insert(0, r"C:/path/to/mcp-unreal/plugins/ue_python_server")
        import ue_server; ue_server.start()   # default: 127.0.0.1:6800

    The server runs as a daemon thread inside UE's embedded Python interpreter,
    so it has full access to the `unreal` module and captures stdout/stderr and
    unreal.log output, returning them in the JSON response.

    This is different from McpAutomationBridge (port 8091), which is a C++ plugin
    for structured editor operations and does NOT expose a Python REPL.

    Default bridge_url: ``http://127.0.0.1:6800``  (matches ue_server.py DEFAULT_PORT)
    """

    DEFAULT_URL = "http://127.0.0.1:6800"

    def __init__(
        self,
        bridge_url: str = DEFAULT_URL,
        timeout: float = _COMMAND_TIMEOUT_S,
    ) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self.timeout = timeout

    def run(
        self,
        code: str,
        exec_mode: str = ExecMode.EXECUTE_STATEMENT,
        unattended: bool = True,
    ) -> ExecResult:
        """POST *code* to ``/execute_python`` on the ue_server.py HTTP server."""
        try:
            import httpx
        except ImportError:
            return ExecResult(
                success=False,
                return_value=None,
                output=[],
                error="httpx is required for the bridge backend. Run: uv add httpx",
            )

        try:
            response = httpx.post(
                f"{self.bridge_url}/execute_python",
                json={
                    "command": code,
                    "exec_mode": exec_mode,
                    "unattended": unattended,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            return ExecResult(
                success=bool(data.get("success", False)),
                return_value=data.get("result"),
                output=data.get("output", []),
            )
        except Exception as exc:  # noqa: BLE001
            return ExecResult(
                success=False,
                return_value=None,
                output=[],
                error=(
                    f"Bridge connection failed: {exc}\n"
                    "Ensure ue_server.py is running inside the UE editor Python console:\n"
                    "  import sys; sys.path.insert(0, r'<mcp-unreal>/plugins/ue_python_server')\n"
                    "  import ue_server; ue_server.start()"
                ),
            )

    def ping(self) -> bool:
        """Return True if ue_server.py is reachable."""
        try:
            import httpx
            r = httpx.get(f"{self.bridge_url}/ping", timeout=3.0)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_client(
    host: Optional[str] = None,
    port: Optional[int] = None,
    bridge_url: Optional[str] = None,
    discovery_timeout: float = _DISCOVERY_TIMEOUT_S,
    command_timeout: float = _COMMAND_TIMEOUT_S,
) -> RemoteExecutionClient | AutomationBridgeClient:
    """
    Return the appropriate client based on the provided parameters.

    - If *bridge_url* is given → AutomationBridgeClient
    - Otherwise              → RemoteExecutionClient (native UDP/TCP)
    """
    if bridge_url:
        return AutomationBridgeClient(bridge_url=bridge_url, timeout=command_timeout)
    return RemoteExecutionClient(
        host=host,
        port=port,
        discovery_timeout=discovery_timeout,
        command_timeout=command_timeout,
    )
