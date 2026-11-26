"""End-to-end test: a real MCP client drives real GDB sessions over stdio.

    .venv/bin/python -u tests/test_mcp.py

Covers the whole surface: a launched program (breakpoints, stepping, stack,
variables, memory, registers), two sessions side by side, attaching to a running
PID, opening a core dump, ~/.gdbinit loading, and every failure path that should
answer ok=false instead of raising.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  -> {detail}" if detail != "" else ""), flush=True)
    if not condition:
        failures.append(label)


def why(result: dict) -> str:
    if result.get("ok") is False:
        return f"ok=false: {result.get('error', '')[:140]}"
    return ""


def payload(result: object) -> dict:
    """Pull the dict back out of a CallToolResult (structured or text block)."""
    for attribute in ("structuredContent", "structured_content"):
        value = getattr(result, attribute, None)
        if isinstance(value, dict) and value:
            return value
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


def build(source: Path, binary: Path) -> None:
    subprocess.run(["gcc", "-g", "-O0", "-o", str(binary), str(source)], check=True)


async def main() -> int:
    binaries = {}
    for name in ("demo", "demo2", "demo_loop"):
        binary = TESTS / name
        build(TESTS / f"{name}.c", binary)
        binaries[name] = binary
    print(f"built {len(binaries)} programs", flush=True)

    # A throwaway HOME whose .gdbinit proves that init loading really happens.
    home = Path(tempfile.mkdtemp(prefix="gdb-mcp-home-"))
    (home / ".gdbinit").write_text("set $mcpinit = 42\nset pagination off\n")

    loop_process = subprocess.Popen(
        [str(binaries["demo_loop"])], stdout=subprocess.PIPE, text=True
    )
    loop_pid = loop_process.pid
    line = loop_process.stdout.readline() if loop_process.stdout else ""
    print(f"started demo_loop pid={loop_pid}: {line.strip()}", flush=True)

    core_path = Path(tempfile.mkdtemp(prefix="gdb-mcp-core-")) / "demo_loop.core"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gdb_mcp.server"],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT), "HOME": str(home)},
    )

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:

                async def call(tool: str, **arguments) -> dict:
                    return payload(await session.call_tool(tool, arguments))

                await session.initialize()
                tools = await session.list_tools()
                names = sorted(tool.name for tool in tools.tools)
                print(f"server exposes {len(names)} tools: {', '.join(names)}", flush=True)
                check("tools listed", len(names) >= 24, len(names))
                for required in ("gdb_start", "gdb_attach", "gdb_load_core", "gdb_sessions",
                                 "gdb_detach", "gdb_close_session", "gdb_evaluate", "gdb_command"):
                    check(f"tool {required} present", required in names)

                # ---------------------------------------------------- session 1
                start = await call("gdb_start", program=str(binaries["demo"]))
                check("gdb_start", start.get("ok") is True, start.get("program") or why(start))
                check("start assigns a session id", start.get("session_id") == "1", start.get("session_id"))
                check("start reports kind launched", start.get("kind") == "launched", start.get("kind"))

                init_value = await call("gdb_evaluate", expression="$mcpinit")
                check("~/.gdbinit was loaded", init_value.get("value") == "42",
                      init_value.get("value") or why(init_value))

                bp = await call("gdb_breakpoint", location="accumulate")
                check("breakpoint set", bp.get("ok") is True and bp.get("number") == "1",
                      bp.get("location") or why(bp))

                run = await call("gdb_run")
                stop = run.get("last_stop") or {}
                check("run stopped at breakpoint", run.get("ok") is True and stop.get("reason") == "breakpoint",
                      stop or why(run))
                check("stopped inside accumulate", stop.get("function") == "accumulate", stop.get("function"))

                variables = await call("gdb_variables")
                seen = {v["name"] for v in variables.get("variables", [])}
                check("arguments visible", {"counter", "value"} <= seen, sorted(seen))

                evaluated = await call("gdb_evaluate", expression="value * 10")
                check("evaluate", evaluated.get("value") == "30", evaluated.get("value"))

                stepped = await call("gdb_next")
                check("next advances a line", stepped.get("ok") is True,
                      (stepped.get("last_stop") or {}).get("line"))

                helper = await call("gdb_breakpoint", location="add")
                check("second breakpoint", helper.get("ok") is True, helper.get("number"))
                continued = await call("gdb_continue")
                check("continue reaches add", (continued.get("last_stop") or {}).get("function") == "add",
                      (continued.get("last_stop") or {}).get("function"))

                stack = await call("gdb_stack", limit=10)
                frames = [f["function"] for f in stack.get("frames", [])]
                check("stack shows the call chain", frames[:2] == ["add", "accumulate"], frames[:3])

                memory = await call("gdb_read_memory", address="$rsp", count=8)
                check("read memory", memory.get("ok") is True and len(memory.get("hex", "")) == 16,
                      memory.get("hex") or why(memory))

                registers = await call("gdb_registers")
                reg_names = {r["name"] for r in registers.get("registers", [])}
                check("registers read", registers.get("count", 0) > 10 and "rsp" in reg_names,
                      registers.get("count"))

                console = await call("gdb_command", command="info threads")
                check("console command", console.get("ok") is True and "Thread" in console.get("output", ""),
                      console.get("output", "")[:50].replace("\n", " "))

                listed = await call("gdb_breakpoints")
                check("breakpoints listed", len(listed.get("breakpoints", [])) == 2,
                      [b["number"] for b in listed.get("breakpoints", [])])

                deleted = await call("gdb_delete_breakpoint", number="2")
                check("breakpoint deleted", deleted.get("ok") is True, why(deleted))

                finished = await call("gdb_finish")
                check("finish returns to caller",
                      (finished.get("last_stop") or {}).get("function") in ("accumulate", "main"),
                      (finished.get("last_stop") or {}).get("function"))

                error_case = await call("gdb_evaluate", expression="no_such_symbol_here")
                check("bad expression reports an error", error_case.get("ok") is False,
                      error_case.get("error", "")[:50])

                # ------------------------------------------- session 2 (parallel)
                start2 = await call("gdb_start", program=str(binaries["demo2"]))
                check("second session starts", start2.get("ok") is True and start2.get("session_id") == "2",
                      start2.get("session_id") or why(start2))

                ambiguous = await call("gdb_stack")
                check("two sessions demand an explicit id", ambiguous.get("ok") is False
                      and "session_id" in ambiguous.get("error", ""), ambiguous.get("error", "")[:80])

                bp2 = await call("gdb_breakpoint", location="worker", session_id="2")
                check("breakpoint in session 2", bp2.get("ok") is True, bp2.get("location") or why(bp2))
                run2 = await call("gdb_run", session_id="2")
                check("session 2 runs to its own breakpoint",
                      (run2.get("last_stop") or {}).get("function") == "worker",
                      (run2.get("last_stop") or {}).get("function"))
                vars2 = await call("gdb_variables", session_id="2")
                names2 = {v["name"] for v in vars2.get("variables", [])}
                # depth is a file-static global, so it is not a frame variable;
                # the point is that session 2 shows demo2's frame, not demo's.
                check("session 2 has its own variables",
                      {"scale", "scaled"} <= names2 and "counter" not in names2, sorted(names2))

                # Session 1 finished add() earlier, so it sits back in accumulate()
                # while session 2 ran: proof the two inferiors are independent.
                still_one = await call("gdb_stack", session_id="1")
                check("session 1 stayed where it was",
                      (still_one.get("frames") or [{}])[0].get("function") == "accumulate",
                      (still_one.get("frames") or [{}])[0].get("function"))

                listing = await call("gdb_sessions")
                check("sessions listed", listing.get("count") == 2,
                      [(s["session_id"], s["kind"]) for s in listing.get("sessions", [])])

                # ---------------------------------------------------- attach
                attach = await call("gdb_attach", pid=loop_pid)
                check("attach to a running pid", attach.get("ok") is True and attach.get("kind") == "attached",
                      f"session {attach.get('session_id')} kind={attach.get('kind')} {why(attach)}")
                attach_id = attach.get("session_id")
                attached_stack = await call("gdb_stack", session_id=attach_id, limit=12)
                attached_frames = [f["function"] for f in attached_stack.get("frames", [])]
                check("attached stack shows the sleeping loop",
                      any("busy_wait" in f or "sleep" in f or "clock_nanosleep" in f for f in attached_frames),
                      attached_frames[:4])

                # A core from the attached process, so the core test needs no
                # system core_pattern cooperation.
                gcore = await call("gdb_command", command=f"gcore {core_path}", session_id=attach_id)
                check("gcore produced a core file", core_path.exists() and core_path.stat().st_size > 0,
                      f"{core_path.stat().st_size if core_path.exists() else 0} bytes; {gcore.get('output', '')[:60]}")

                detached = await call("gdb_detach", session_id=attach_id)
                check("detach leaves the session alive",
                      detached.get("ok") is True and detached.get("pid") is None, why(detached))
                time.sleep(0.3)
                process_alive = loop_process.poll() is None
                check("target still running after detach", process_alive)

                # ------------------------------------------------------ core
                core_session = await call("gdb_load_core", core_path=str(core_path),
                                          program=str(binaries["demo_loop"]))
                check("core session opens", core_session.get("ok") is True and core_session.get("kind") == "core",
                      f"session {core_session.get('session_id')} kind={core_session.get('kind')} {why(core_session)}")
                core_id = core_session.get("session_id")
                core_stack = await call("gdb_stack", session_id=core_id, limit=10)
                core_frames = [f["function"] for f in core_stack.get("frames", [])]
                check("core has a stack", len(core_frames) >= 2 and "main" in core_frames, core_frames[:5])
                core_regs = await call("gdb_registers", session_id=core_id)
                check("core has registers", core_regs.get("count", 0) > 10, core_regs.get("count"))

                # ------------------------------------------------------ teardown
                for session_key in ("1", "2", attach_id, core_id):
                    closed = await call("gdb_close_session", session_id=session_key)
                    if not closed.get("ok"):
                        check(f"close session {session_key}", False, why(closed))
                remaining = await call("gdb_sessions")
                check("all sessions closed", remaining.get("count") == 0,
                      [s["session_id"] for s in remaining.get("sessions", [])])

                after = await call("gdb_evaluate", expression="1+1")
                check("tools fail cleanly after teardown", after.get("ok") is False,
                      after.get("error", "")[:60])

    finally:
        loop_process.send_signal(signal.SIGKILL)
        loop_process.wait(timeout=5)
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(core_path.parent, ignore_errors=True)

    print(flush=True)
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
