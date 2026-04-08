"""
MCP server definition.

Exposes a single tool:  execute-script
  Executes arbitrary Python code in the running Unreal Engine 5 editor and
  returns the result as a formatted Markdown report.
"""

from __future__ import annotations

import json
import logging
import textwrap
from typing import Any, Optional

import mcp.types as types
from mcp.server import Server

from mcp_unreal.ue_remote import ExecResult, ExecMode, make_client

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Build the MCP application
# ---------------------------------------------------------------------------

def create_server(
    host: Optional[str] = None,
    port: Optional[int] = None,
    bridge_url: Optional[str] = None,
    discovery_timeout: float = 5.0,
    command_timeout: float = 60.0,
) -> Server:
    """
    Create and configure the MCP server instance.

    Parameters mirror those of :func:`mcp_unreal.ue_remote.make_client`.
    """
    app = Server("mcp-unreal")
    client = make_client(
        host=host,
        port=port,
        bridge_url=bridge_url,
        discovery_timeout=discovery_timeout,
        command_timeout=command_timeout,
    )

    # ------------------------------------------------------------------
    # Tool listing
    # ------------------------------------------------------------------

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="execute-script",
                description=(
                    "Execute arbitrary Python code in the running Unreal Engine 5 editor. "
                    "The script runs in the UE Python scripting environment with full access "
                    "to the `unreal` module, editor subsystems, asset library, and level library. "
                    "Use for automation, batch asset operations, level editing, blueprint inspection, "
                    "and any task not covered by dedicated tools. "
                    "Returns all print/log output plus the final return value."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": (
                                "Python source code to execute. "
                                "Has access to the `unreal` module and all editor APIs. "
                                "Use `unreal.log()` or `print()` to capture output. "
                                "The last evaluated expression is captured as the return value "
                                "when exec_mode is 'EvaluateStatement'."
                            ),
                        },
                        "exec_mode": {
                            "type": "string",
                            "enum": [
                                "ExecuteStatement",
                                "EvaluateStatement",
                                "ExecuteFile",
                            ],
                            "default": "ExecuteStatement",
                            "description": (
                                "Execution mode:\n"
                                "- ExecuteStatement: run as exec() – best for multi-line scripts "
                                "and side-effects; captures stdout via print/unreal.log.\n"
                                "- EvaluateStatement: run as eval() – returns an expression value "
                                "directly; use for single-expression queries.\n"
                                "- ExecuteFile: treat `code` as a file path on the UE host to run."
                            ),
                        },
                        "unattended": {
                            "type": "boolean",
                            "default": True,
                            "description": (
                                "Suppress interactive UE dialogs during execution (recommended)."
                            ),
                        },
                    },
                    "required": ["code"],
                },
            )
        ]

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    @app.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        if name != "execute-script":
            raise ValueError(f"Unknown tool: {name}")

        code: str = arguments.get("code", "")
        exec_mode: str = arguments.get("exec_mode", ExecMode.EXECUTE_STATEMENT)
        unattended: bool = arguments.get("unattended", True)

        if not code.strip():
            markdown = _format_error("Empty script: no code was provided.")
            return [types.TextContent(type="text", text=markdown)]

        log.info("Executing %d-char script (mode=%s)", len(code), exec_mode)
        result: ExecResult = client.run(code, exec_mode=exec_mode, unattended=unattended)

        markdown = _format_result(result, code, exec_mode)
        return [types.TextContent(type="text", text=markdown)]

    return app


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def _format_result(result: ExecResult, code: str, exec_mode: str) -> str:
    """Render an ExecResult as a Markdown report."""
    if result.error:
        return _format_error(result.error)

    status = "✅ Success" if result.success else "❌ Failure"

    # --- Output section ---
    stdout = "".join(result.stdout_lines).rstrip()
    warnings = "".join(result.warning_lines).rstrip()
    errors_out = "".join(result.error_lines).rstrip()

    output_section = _build_output_section(stdout, warnings, errors_out)

    # --- Return value section ---
    return_section = _build_return_section(result.return_value, exec_mode)

    # --- Exec mode badge ---
    mode_label = {
        ExecMode.EXECUTE_STATEMENT: "ExecuteStatement",
        ExecMode.EVALUATE_STATEMENT: "EvaluateStatement",
        ExecMode.EXECUTE_FILE: "ExecuteFile",
    }.get(exec_mode, exec_mode)

    # --- Script echo (first 8 lines) ---
    code_preview = _truncate_code(code, max_lines=8)

    parts = [
        "## Script Execution Result",
        "",
        f"**Status:** {status}  |  **Mode:** `{mode_label}`",
        "",
    ]

    parts += output_section
    parts += return_section
    parts += [
        "### Script",
        "",
        "```python",
        code_preview,
        "```",
        "",
        "### About This Tool",
        "",
        textwrap.dedent("""\
            Executes arbitrary Python/Jython code inside the running Unreal Engine 5 \
editor with full access to the `unreal` module. Useful for:
            - Batch asset operations via `unreal.EditorAssetLibrary`
            - Level editing via `unreal.EditorLevelLibrary`
            - Blueprint inspection and modification
            - Custom automation not covered by dedicated MCP tools

            **API references:**
            - [Unreal Python API](https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api)
            - [FlatProgramAPI (C++ scripting reference)](https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/module/unreal)
            - [Scripting the Unreal Editor using Python](https://dev.epicgames.com/documentation/en-us/unreal-engine/scripting-the-unreal-editor-using-python)
        """),
        "",
        "### Suggested Next Steps",
        "",
        "1. Use `unreal.EditorAssetLibrary.find_asset_data()` to locate and inspect assets.",
        "2. Use `unreal.EditorLevelLibrary.get_all_level_actors()` to iterate level actors.",
        "3. Use `unreal.log()` / `unreal.log_warning()` / `unreal.log_error()` for structured output.",
        "4. Chain `execute-script` calls to build multi-step automation pipelines.",
    ]

    return "\n".join(parts)


def _build_output_section(stdout: str, warnings: str, errors: str) -> list[str]:
    lines: list[str] = []

    if stdout or warnings or errors:
        lines += ["### Output", ""]
        if stdout:
            lines += ["```", stdout, "```", ""]
        if warnings:
            lines += ["> **⚠ Warnings**", "> ```", *[f"> {ln}" for ln in warnings.splitlines()], "> ```", ""]
        if errors:
            lines += ["> **🔴 Errors**", "> ```", *[f"> {ln}" for ln in errors.splitlines()], "> ```", ""]
    else:
        lines += ["### Output", "", "_No output produced._", ""]

    return lines


def _build_return_section(return_value: Any, exec_mode: str) -> list[str]:
    lines: list[str] = ["### Return Value", ""]

    if return_value is None:
        lines += ["`None`", ""]
        return lines

    # Try to pretty-print as JSON (handles lists, dicts, numbers, booleans)
    try:
        # UE returns repr() strings – try to eval them to a JSON-safe object first
        parsed = _safe_eval_repr(return_value)
        pretty = json.dumps(parsed, indent=2, default=str)
        lines += ["```json", pretty, "```", ""]
    except Exception:  # noqa: BLE001
        # Fall back to raw string output
        lines += ["```", str(return_value), "```", ""]

    return lines


def _safe_eval_repr(value: str) -> Any:
    """
    Attempt to parse a Python repr() string into a JSON-compatible value.
    Only permits literals (str, int, float, bool, None, list, dict, tuple).
    """
    import ast

    tree = ast.parse(value, mode="eval")
    return _ast_to_value(tree.body)


def _ast_to_value(node: Any) -> Any:
    import ast

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_ast_to_value(el) for el in node.elts]
    if isinstance(node, ast.Tuple):
        return [_ast_to_value(el) for el in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _ast_to_value(k): _ast_to_value(v)
            for k, v in zip(node.keys, node.values)
        }
    if isinstance(node, ast.Set):
        return [_ast_to_value(el) for el in node.elts]
    if isinstance(node, (ast.UnaryOp)) and isinstance(node.op, ast.USub):
        return -_ast_to_value(node.operand)
    # Not a safe literal – fall back
    raise ValueError(f"Unsupported AST node: {type(node).__name__}")


def _truncate_code(code: str, max_lines: int) -> str:
    lines = code.splitlines()
    if len(lines) <= max_lines:
        return code
    shown = "\n".join(lines[:max_lines])
    return f"{shown}\n... ({len(lines) - max_lines} more lines)"


def _format_error(message: str) -> str:
    return "\n".join([
        "## Script Execution Result",
        "",
        "**Status:** ❌ Error",
        "",
        "### Error",
        "",
        "```",
        message,
        "```",
        "",
        "### Troubleshooting",
        "",
        "- Ensure Unreal Engine is running with the **Python Script Plugin** enabled.",
        "- In Project Settings → Plugins → Python, enable **Remote Execution**.",
        "- Check that the firewall allows UDP multicast (239.0.0.1:6766) and the TCP command port.",
        "- If using `--host`/`--port`, confirm the UE remote command port matches.",
        "- For the Automation Bridge backend (`--bridge-url`), ensure the "
        "McpAutomationBridge plugin is installed and UE is running.",
    ])
