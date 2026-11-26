"""MCP server that drives GDB through pygdbmi.

Run it over stdio (the default) and point any MCP client at it:

    python -m gdb_mcp.server

Claude Desktop / Hermes style configuration:

    {"mcpServers": {"gdb": {"command": "python", "args": ["-m", "gdb_mcp.server"]}}}

Tools mirror what you actually do at a debugger prompt: start a session, load a
binary, set breakpoints, run and step, then look at the stack, variables,
registers and memory. ``gdb_command`` is the escape hatch for everything else.
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from .session import GdbError, GdbSession

# mcp 2.x renamed FastMCP to MCPServer (mcp.server.mcpserver). The decorator and
# stdio transport keep the same shape; pinning mcp<2 would also work but then the
# server is built on a deprecated API from day one.
mcp = MCPServer("gdb")
session = GdbSession()


def _error(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


@mcp.tool()
def gdb_start(program: str | None = None, args: list[str] | None = None) -> dict[str, Any]:
    """Start a GDB session (once per server process), optionally loading a binary.

    Args:
        program: Path to the executable to debug.
        args: Arguments passed to the program when it is run.
    """
    try:
        state = session.start(program=program, args=args, gdb_path=os.environ.get("GDB_PATH", "gdb"))
        return {"ok": True, **state}
    except Exception as exc:  # noqa: BLE001 - surfaced to the client verbatim
        return _error(exc)


@mcp.tool()
def gdb_load(program: str) -> dict[str, Any]:
    """Load an executable (or core file) into the running GDB session."""
    try:
        session.start(program=program)
        return {"ok": True, **session.status()}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_breakpoint(location: str) -> dict[str, Any]:
    """Set a breakpoint. ``location`` is a function, file:line, or *address."""
    try:
        return {"ok": True, **session.breakpoint(location)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_delete_breakpoint(number: str) -> dict[str, Any]:
    """Delete a breakpoint by its number."""
    try:
        return {"ok": True, **session.delete_breakpoint(number)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_breakpoints() -> dict[str, Any]:
    """List the breakpoints currently set."""
    try:
        return {"ok": True, "breakpoints": session.breakpoints()}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_run(args: list[str] | None = None, timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Start the program and wait for it to stop (breakpoint, signal or exit)."""
    try:
        return {"ok": True, **session.run(args=args, timeout=timeout_seconds)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_continue(timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Resume execution and wait for the next stop."""
    try:
        return {"ok": True, **session.resume(timeout=timeout_seconds)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_step(timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Step into the next source line."""
    return _step("step", timeout_seconds)


@mcp.tool()
def gdb_next(timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Step over the next source line."""
    return _step("next", timeout_seconds)


@mcp.tool()
def gdb_finish(timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Run until the current function returns."""
    return _step("finish", timeout_seconds)


@mcp.tool()
def gdb_step_instruction(timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Step one machine instruction."""
    return _step("stepi", timeout_seconds)


@mcp.tool()
def gdb_interrupt() -> dict[str, Any]:
    """Interrupt a running program and report where it stopped."""
    try:
        return {"ok": True, **session.interrupt()}
    except Exception as exc:
        return _error(exc)


def _step(kind: str, timeout_seconds: float) -> dict[str, Any]:
    try:
        return {"ok": True, **session.step(kind=kind, timeout=timeout_seconds)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_stack(limit: int = 20) -> dict[str, Any]:
    """Return the call stack, innermost frame first."""
    try:
        return {"ok": True, "frames": session.stack(limit=limit)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_variables(frame: int | None = None) -> dict[str, Any]:
    """List the variables visible in a stack frame (default: the current one)."""
    try:
        return {"ok": True, "variables": session.variables(frame=frame)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_evaluate(expression: str) -> dict[str, Any]:
    """Evaluate a GDB expression in the current frame, e.g. ``count * 2``."""
    try:
        return {"ok": True, **session.evaluate(expression)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_registers() -> dict[str, Any]:
    """Read the general-purpose registers as hex."""
    try:
        return {"ok": True, **session.registers()}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_read_memory(address: str, count: int = 16) -> dict[str, Any]:
    """Read ``count`` bytes at ``address`` (a symbol, expression or hex address)."""
    try:
        return {"ok": True, **session.memory(address, count)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_command(command: str) -> dict[str, Any]:
    """Run any GDB console command, e.g. ``info threads`` or ``x/8xb $rsp``."""
    try:
        return {"ok": True, **session.command(command)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_status() -> dict[str, Any]:
    """Report whether a session is live, what is loaded, and where it stopped."""
    return {"ok": True, **session.status()}


@mcp.tool()
def gdb_stop() -> dict[str, Any]:
    """Terminate the GDB session and free its process."""
    return {"ok": True, **session.stop()}


def main() -> None:
    # stdio is what every MCP client expects by default; keep the log quiet so
    # stray prints cannot corrupt the JSON-RPC stream.
    mcp.run()


if __name__ == "__main__":
    main()
