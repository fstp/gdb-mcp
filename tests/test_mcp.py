"""End-to-end test: a real MCP client drives a real GDB over stdio.

Run it with the venv python from the repo root:

    python tests/test_mcp.py

It builds tests/demo.c, starts the server as a subprocess, and walks the whole
tool surface: start, breakpoint, run, variables, evaluate, step, stack, memory,
registers, a console command, then stop. Any tool that answers ok=false fails the
run, so this is the same path an editor or agent would take.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
DEMO_SOURCE = ROOT / "tests" / "demo.c"
DEMO_BINARY = ROOT / "tests" / "demo"

failures: list[str] = []


def why(result: dict) -> str:
    """Short reason string for a failed call, so failures are self-explaining."""
    if result.get("ok") is False:
        return f"ok=false: {result.get('error', '')[:120]}"
    return ""


def check(label: str, condition: bool, detail: object = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  -> {detail}" if detail != "" else ""))
    if not condition:
        failures.append(label)


def payload(result: object) -> dict:
    """Pull the dict back out of a CallToolResult.

    A tool that returns a dict may come back either as structured content (mcp
    2.x) or as a JSON text block (older clients), so accept both.
    """
    for attribute in ("structuredContent", "structured_content"):
        value = getattr(result, attribute, None)
        if isinstance(value, dict) and value:
            return value
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


async def main() -> int:
    print(f"building {DEMO_SOURCE.name} with gcc -g -O0 ...")
    subprocess.run(["gcc", "-g", "-O0", "-o", str(DEMO_BINARY), str(DEMO_SOURCE)], check=True)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gdb_mcp.server"],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            print(f"server exposes {len(names)} tools: {', '.join(names)}")
            check("tools listed", len(names) >= 18, len(names))
            for required in ("gdb_start", "gdb_breakpoint", "gdb_run", "gdb_evaluate", "gdb_stack", "gdb_command"):
                check(f"tool {required} present", required in names)

            start = payload(await session.call_tool("gdb_start", {"program": str(DEMO_BINARY)}))
            check("gdb_start", start.get("ok") is True, start.get("program") or why(start))
            check("start reports not running", start.get("running") is False)

            bp = payload(await session.call_tool("gdb_breakpoint", {"location": "accumulate"}))
            check("breakpoint set", bp.get("ok") is True and bp.get("number") == "1", bp.get("location") or why(bp))

            run = payload(await session.call_tool("gdb_run", {}))
            stop = run.get("last_stop") or {}
            check("run stopped at breakpoint", run.get("ok") is True and stop.get("reason") == "breakpoint", stop or why(run))
            check("stopped inside accumulate", stop.get("function") == "accumulate", stop.get("function"))

            variables = payload(await session.call_tool("gdb_variables", {}))
            names_seen = {v["name"] for v in variables.get("variables", [])}
            check("arguments visible", {"counter", "value"} <= names_seen, sorted(names_seen))

            evaluated = payload(await session.call_tool("gdb_evaluate", {"expression": "value * 10"}))
            check("evaluate", evaluated.get("value") == "30", evaluated.get("value"))

            stepped = payload(await session.call_tool("gdb_next", {}))
            check("next advances a line", stepped.get("ok") is True, (stepped.get("last_stop") or {}).get("line"))

            helper = payload(await session.call_tool("gdb_breakpoint", {"location": "add"}))
            check("second breakpoint", helper.get("ok") is True, helper.get("number"))
            continued = payload(await session.call_tool("gdb_continue", {}))
            check("continue reaches add", (continued.get("last_stop") or {}).get("function") == "add",
                  (continued.get("last_stop") or {}).get("function"))

            stack = payload(await session.call_tool("gdb_stack", {"limit": 10}))
            frames = [f["function"] for f in stack.get("frames", [])]
            check("stack shows the call chain", frames[:2] == ["add", "accumulate"], frames[:3])

            # $rsp rather than a local: at this point the frame is inside add(),
            # where main's `counter` is not in scope. (Same reason a user would
            # reach for a register.)
            memory = payload(await session.call_tool("gdb_read_memory", {"address": "$rsp", "count": 8}))
            check("read memory", memory.get("ok") is True and len(memory.get("hex", "")) == 16,
                  memory.get("hex") or why(memory))

            registers = payload(await session.call_tool("gdb_registers", {}))
            reg_names = {r["name"] for r in registers.get("registers", [])}
            check("registers read", registers.get("count", 0) > 10 and "rsp" in reg_names, registers.get("count"))

            console = payload(await session.call_tool("gdb_command", {"command": "info threads"}))
            check("console command", console.get("ok") is True and "Thread" in console.get("output", ""),
                  console.get("output", "")[:60].replace("\n", " "))

            listed = payload(await session.call_tool("gdb_breakpoints", {}))
            check("breakpoints listed", len(listed.get("breakpoints", [])) == 2, listed.get("breakpoints"))

            deleted = payload(await session.call_tool("gdb_delete_breakpoint", {"number": "2"}))
            check("breakpoint deleted", deleted.get("ok") is True)

            finished = payload(await session.call_tool("gdb_finish", {}))
            check("finish returns to caller",
                  (finished.get("last_stop") or {}).get("function") in ("accumulate", "main"),
                  (finished.get("last_stop") or {}).get("function"))

            error_case = payload(await session.call_tool("gdb_evaluate", {"expression": "no_such_symbol_here"}))
            check("bad expression reports an error", error_case.get("ok") is False, error_case.get("error", "")[:60])

            stopped = payload(await session.call_tool("gdb_stop", {}))
            check("session stopped", stopped.get("active") is False)

            after = payload(await session.call_tool("gdb_evaluate", {"expression": "1+1"}))
            check("tools fail cleanly after stop", after.get("ok") is False, after.get("error", "")[:60])

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
