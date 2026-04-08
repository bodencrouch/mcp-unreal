"""
CLI entry point for mcp-unreal.

Usage
-----
  mcp-unreal                                  # stdio transport, UDP discovery
  mcp-unreal --host 127.0.0.1 --port 6969     # stdio + direct TCP endpoint
  mcp-unreal --transport http --port 8080     # HTTP/SSE MCP transport on port 8080
  mcp-unreal --bridge-url http://localhost:8091  # use McpAutomationBridge plugin
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-unreal",
        description=(
            "MCP server for Unreal Engine 5 Python scripting.\n\n"
            "Connects to a running UE5 editor via the Python Remote Execution protocol "
            "(or the McpAutomationBridge plugin) and exposes a single `execute-script` tool."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ---------------------------------------------------------------
    # Unreal connection options
    # ---------------------------------------------------------------
    ue_group = parser.add_argument_group("Unreal Engine connection")
    ue_group.add_argument(
        "--host",
        metavar="HOST",
        default=os.environ.get("UE_HOST"),
        help=(
            "IP or hostname of the machine running UE5. "
            "When omitted, UDP multicast discovery is used to find a local UE node. "
            "Requires --ue-port when specified. "
            "Env: UE_HOST."
        ),
    )
    ue_group.add_argument(
        "--ue-port",
        metavar="PORT",
        type=int,
        default=int(os.environ["UE_PORT"]) if os.environ.get("UE_PORT") else None,
        help=(
            "TCP command port of the UE5 remote execution listener. "
            "Only required when --host is given (discovery provides the port automatically)."
        ),
    )
    ue_group.add_argument(
        "--bridge-url",
        metavar="URL",
        default=os.environ.get("UE_BRIDGE_URL"),
        help=(
            "HTTP base URL of the ue_server.py Python execution server running inside UE, e.g. "
            "http://127.0.0.1:6800 (the default port used by ue_server.py). "
            "Env: UE_BRIDGE_URL. "
            "Start the server once from the UE editor Python console: "
            "  import sys; sys.path.insert(0, r'<mcp-unreal>/plugins/ue_python_server'); "
            "  import ue_server; ue_server.start(). "
            "When set, all execute-script calls are routed to this server instead of "
            "the native UDP/TCP remote-execution protocol. "
            "Note: this is NOT the McpAutomationBridge plugin (port 8091), which does "
            "not expose a Python REPL endpoint."
        ),
    )
    ue_group.add_argument(
        "--discovery-timeout",
        metavar="SECONDS",
        type=float,
        default=float(os.environ.get("UE_DISCOVERY_TIMEOUT", 5.0)),
        help="Seconds to wait for a UE node during UDP multicast discovery (default: 5). Env: UE_DISCOVERY_TIMEOUT.",
    )
    ue_group.add_argument(
        "--command-timeout",
        metavar="SECONDS",
        type=float,
        default=float(os.environ.get("UE_COMMAND_TIMEOUT", 60.0)),
        help="Seconds to wait for a script result after sending it to UE (default: 60). Env: UE_COMMAND_TIMEOUT.",
    )

    # ---------------------------------------------------------------
    # MCP transport options
    # ---------------------------------------------------------------
    transport_group = parser.add_argument_group("MCP transport")
    transport_group.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help=(
            "MCP transport to use:\n"
            "  stdio  – standard input/output (default, for Claude Desktop / VS Code)\n"
            "  http   – HTTP with JSON-RPC\n"
            "  sse    – HTTP with Server-Sent Events (legacy clients)"
        ),
    )
    transport_group.add_argument(
        "--port",
        metavar="PORT",
        type=int,
        default=int(os.environ.get("MCP_PORT", 8080)),
        help="Port to listen on when using http or sse transport (default: 8080). Env: MCP_PORT.",
    )
    transport_group.add_argument(
        "--bind",
        metavar="HOST",
        default=os.environ.get("MCP_BIND", "127.0.0.1"),
        help="Host to bind to when using http or sse transport (default: 127.0.0.1). Env: MCP_BIND.",
    )

    # ---------------------------------------------------------------
    # Logging
    # ---------------------------------------------------------------
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.environ.get("MCP_LOG_LEVEL", "WARNING"),
        help="Logging verbosity (default: WARNING). Env: MCP_LOG_LEVEL.",
    )

    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    log = logging.getLogger("mcp_unreal.cli")
    log.debug("Starting mcp-unreal with args: %s", args)

    from mcp_unreal.server import create_server

    server = create_server(
        host=args.host,
        port=args.ue_port,
        bridge_url=args.bridge_url,
        discovery_timeout=args.discovery_timeout,
        command_timeout=args.command_timeout,
    )

    asyncio.run(_run(server, args))


async def _run(server, args) -> None:
    transport: str = args.transport

    if transport == "stdio":
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    elif transport in ("http", "sse"):
        try:
            from mcp.server.sse import SseServerTransport
            from starlette.applications import Starlette
            from starlette.routing import Route, Mount
            import uvicorn
        except ImportError as exc:
            print(
                f"HTTP/SSE transport requires additional dependencies: {exc}\n"
                "Install with:  uv add 'mcp[http]' uvicorn starlette",
                file=__import__("sys").stderr,
            )
            raise SystemExit(1) from exc

        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await server.run(
                    streams[0],
                    streams[1],
                    server.create_initialization_options(),
                )

        starlette_app = Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ]
        )

        config = uvicorn.Config(
            starlette_app,
            host=args.bind,
            port=args.port,
            log_level=args.log_level.lower(),
        )
        uv_server = uvicorn.Server(config)
        await uv_server.serve()

    else:
        raise ValueError(f"Unknown transport: {transport}")
