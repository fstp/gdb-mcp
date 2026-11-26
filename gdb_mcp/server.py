"""MCP server that drives GDB through pygdbmi.

Run it over stdio (the default) and point any MCP client at it:

    python -m gdb_mcp.server

Client configuration:

    {"mcpServers": {"gdb": {"command": "python", "args": ["-m", "gdb_mcp.server"]}}}

Tools mirror what you actually do at a debugger prompt: open a session (launch a
program, attach to a PID, or open a core file), set breakpoints, run and step,
then look at the stack, variables, registers and memory. Several sessions can be
open at once, each addressed by ``session_id``; with a single session open the id
can be omitted. ``gdb_command`` is the escape hatch for everything else.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from .session import GdbError, SessionManager

# mcp 2.x renamed FastMCP to MCPServer (mcp.server.mcpserver). The decorator and
# stdio transport keep the same shape.
mcp = MCPServer("gdb")
sessions = SessionManager()


def _error(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


def _gdb_path() -> str:
    return os.environ.get("GDB_PATH", "gdb")


def _init_default() -> bool:
    """~/.gdbinit is loaded unless GDB_MCP_INIT=0 says otherwise."""
    return os.environ.get("GDB_MCP_INIT", "1") not in ("0", "false", "no")


# --------------------------------------------------------------------- sessions


@mcp.tool()
def gdb_start(program: str | None = None, args: list[str] | None = None,
              load_init: bool | None = None) -> dict[str, Any]:
    """Open a new session, optionally loading a program.

    Args:
        program: Path to the executable to debug.
        args: Arguments passed to the program when it is run.
        load_init: Load ~/.gdbinit (GEF, pwndbg, your own helpers). Defaults to
            on; pass false for a bare GDB.
    """
    try:
        session = sessions.create(
            program=program,
            args=args,
            load_init=_init_default() if load_init is None else load_init,
            gdb_path=_gdb_path(),
        )
        return {"ok": True, **session.status()}
    except Exception as exc:  # noqa: BLE001 - surfaced to the client verbatim
        return _error(exc)


@mcp.tool()
def gdb_attach(pid: int, load_init: bool | None = None) -> dict[str, Any]:
    """Attach to a running process by PID, in a new session.

    The process keeps running if you later call gdb_detach.
    """
    try:
        session = sessions.attach(
            int(pid),
            load_init=_init_default() if load_init is None else load_init,
            gdb_path=_gdb_path(),
        )
        return {"ok": True, **session.status()}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_load_core(core_path: str, program: str | None = None,
                  load_init: bool | None = None) -> dict[str, Any]:
    """Open a core dump in a new session, optionally with its executable."""
    try:
        session = sessions.load_core(
            core_path,
            program=program,
            load_init=_init_default() if load_init is None else load_init,
            gdb_path=_gdb_path(),
        )
        return {"ok": True, **session.status()}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_sessions() -> dict[str, Any]:
    """List every session: id, kind (launched/attached/core), program, state."""
    try:
        listing = sessions.list()
        return {"ok": True, "count": len(listing), "sessions": listing}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_detach(session_id: str | None = None) -> dict[str, Any]:
    """Detach from an attached process, leaving it running."""
    try:
        return {"ok": True, **sessions.resolve(session_id).detach()}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_close_session(session_id: str | None = None) -> dict[str, Any]:
    """Close a session and free its GDB process."""
    try:
        return {"ok": True, **sessions.close(session_id)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_stop(session_id: str | None = None) -> dict[str, Any]:
    """Alias for gdb_close_session, kept so older prompts keep working."""
    return gdb_close_session(session_id)


# ---------------------------------------------------------------------- loading


@mcp.tool()
def gdb_load(program: str, session_id: str | None = None) -> dict[str, Any]:
    """Load (or replace) the executable in an existing session."""
    try:
        session = sessions.resolve(session_id)
        session.start(program=program, gdb_path=_gdb_path())
        return {"ok": True, **session.status()}
    except Exception as exc:
        return _error(exc)


# ----------------------------------------------------------------- breakpoints


@mcp.tool()
def gdb_breakpoint(location: str, session_id: str | None = None) -> dict[str, Any]:
    """Set a breakpoint. ``location`` is a function, file:line, or *address."""
    try:
        return {"ok": True, **sessions.resolve(session_id).breakpoint(location)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_delete_breakpoint(number: str, session_id: str | None = None) -> dict[str, Any]:
    """Delete a breakpoint by its number."""
    try:
        return {"ok": True, **sessions.resolve(session_id).delete_breakpoint(number)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_breakpoints(session_id: str | None = None) -> dict[str, Any]:
    """List the breakpoints in a session."""
    try:
        return {"ok": True, "breakpoints": sessions.resolve(session_id).breakpoints()}
    except Exception as exc:
        return _error(exc)


# --------------------------------------------------------------------- execution


@mcp.tool()
def gdb_run(args: list[str] | None = None, timeout_seconds: float = 10.0,
            session_id: str | None = None) -> dict[str, Any]:
    """Start the program and wait for it to stop (breakpoint, signal or exit)."""
    try:
        return {"ok": True, **sessions.resolve(session_id).run(args=args, timeout=timeout_seconds)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_continue(timeout_seconds: float = 10.0, session_id: str | None = None) -> dict[str, Any]:
    """Resume execution and wait for the next stop."""
    try:
        return {"ok": True, **sessions.resolve(session_id).resume(timeout=timeout_seconds)}
    except Exception as exc:
        return _error(exc)


def _step(kind: str, timeout_seconds: float, session_id: str | None) -> dict[str, Any]:
    try:
        return {"ok": True, **sessions.resolve(session_id).step(kind=kind, timeout=timeout_seconds)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_step(timeout_seconds: float = 10.0, session_id: str | None = None) -> dict[str, Any]:
    """Step into the next source line."""
    return _step("step", timeout_seconds, session_id)


@mcp.tool()
def gdb_next(timeout_seconds: float = 10.0, session_id: str | None = None) -> dict[str, Any]:
    """Step over the next source line."""
    return _step("next", timeout_seconds, session_id)


@mcp.tool()
def gdb_finish(timeout_seconds: float = 10.0, session_id: str | None = None) -> dict[str, Any]:
    """Run until the current function returns."""
    return _step("finish", timeout_seconds, session_id)


@mcp.tool()
def gdb_step_instruction(timeout_seconds: float = 10.0, session_id: str | None = None) -> dict[str, Any]:
    """Step one machine instruction."""
    return _step("stepi", timeout_seconds, session_id)


@mcp.tool()
def gdb_interrupt(session_id: str | None = None) -> dict[str, Any]:
    """Interrupt a running program and report where it stopped."""
    try:
        return {"ok": True, **sessions.resolve(session_id).interrupt()}
    except Exception as exc:
        return _error(exc)


# -------------------------------------------------------------------- inspection


@mcp.tool()
def gdb_stack(limit: int = 20, session_id: str | None = None) -> dict[str, Any]:
    """Return the call stack, innermost frame first."""
    try:
        return {"ok": True, "frames": sessions.resolve(session_id).stack(limit=limit)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_variables(frame: int | None = None, session_id: str | None = None) -> dict[str, Any]:
    """List the variables in a stack frame (default: the current one)."""
    try:
        return {"ok": True, "variables": sessions.resolve(session_id).variables(frame=frame)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_evaluate(expression: str, session_id: str | None = None) -> dict[str, Any]:
    """Evaluate a GDB expression in the current frame, e.g. ``count * 2``."""
    try:
        return {"ok": True, **sessions.resolve(session_id).evaluate(expression)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_registers(session_id: str | None = None) -> dict[str, Any]:
    """Read the general-purpose registers as hex."""
    try:
        return {"ok": True, **sessions.resolve(session_id).registers()}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_read_memory(address: str, count: int = 16, session_id: str | None = None) -> dict[str, Any]:
    """Read ``count`` bytes at ``address`` (a symbol, expression or hex address)."""
    try:
        return {"ok": True, **sessions.resolve(session_id).memory(address, count)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_command(command: str, session_id: str | None = None) -> dict[str, Any]:
    """Run any GDB console command, e.g. ``info threads`` or ``x/8xb $rsp``."""
    try:
        return {"ok": True, **sessions.resolve(session_id).command(command)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def gdb_status(session_id: str | None = None) -> dict[str, Any]:
    """State of one session, or of every session when no id is given."""
    try:
        if session_id is None:
            listing = sessions.list()
            if len(listing) == 1:
                return {"ok": True, **listing[0]}
            return {"ok": True, "count": len(listing), "sessions": listing}
        return {"ok": True, **sessions.resolve(session_id).status()}
    except Exception as exc:
        return _error(exc)


def main() -> None:
    # stdio is what every MCP client expects by default; keep the log quiet so
    # stray prints cannot corrupt the JSON-RPC stream.
    try:
        mcp.run()
    finally:
        sessions.close_all()


if __name__ == "__main__":
    main()
