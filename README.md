# gdb-mcp

An MCP server that lets an MCP client (Claude Desktop, Hermes, an editor agent)
drive GDB: launch a program, attach to a running process, open a core dump, set
breakpoints, run and step, then look at the stack, variables, registers and
memory. Several targets can be debugged at once in separate sessions.

Built on [pygdbmi](https://github.com/cs01/pygdbmi), which speaks GDB/MI over a
pipe, and the official `mcp` Python SDK.

## Why

Debugging from an agent is a loop of "run it, look at the frame, print the
variable". Every step of that loop is a text command to GDB, so it maps onto
tools cleanly, as long as execution control waits for the actual stop event
instead of returning as soon as GDB accepts `run`. That is the one subtle bit
here: `-exec-run` answers `^running`, and the interesting answer is the
`*stopped` record that arrives afterwards.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

GDB must be on PATH (override with `GDB_PATH`).

## Run

```sh
.venv/bin/python -m gdb_mcp.server
```

Client configuration (Claude Desktop style):

```json
{
  "mcpServers": {
    "gdb": {
      "command": "/path/to/gdb-mcp/.venv/bin/python",
      "args": ["-m", "gdb_mcp.server"],
      "cwd": "/path/to/gdb-mcp"
    }
  }
}
```

## Sessions

A session is one GDB process. Open one per target:

| Tool | What it does |
| --- | --- |
| `gdb_start(program?, args?, load_init?)` | New session, optionally loading a binary |
| `gdb_attach(pid, load_init?)` | New session attached to a running process |
| `gdb_load_core(core_path, program?, load_init?)` | New session on a core dump |
| `gdb_sessions()` | Every session: id, kind, program, state |
| `gdb_detach(session_id?)` | Detach from the process, leaving it running |
| `gdb_close_session(session_id?)` / `gdb_stop` | Close a session and free its GDB |

Every other tool takes an optional `session_id`. With a single session open it
can be omitted; with several open the server refuses to guess and lists the ids
instead. Sessions are independent: breakpoints, stops and loaded binaries do not
leak from one to another.

`~/.gdbinit` is loaded by default, so GEF, pwndbg and your own helpers keep
working. Pass `load_init=false` (or set `GDB_MCP_INIT=0`) for a bare GDB.

## Tools

| Tool | What it does |
| --- | --- |
| `gdb_load(program, session_id?)` | Load or replace the executable in a session |
| `gdb_breakpoint(location, session_id?)` | Break on a function, `file:line` or `*address` |
| `gdb_delete_breakpoint(number, session_id?)` / `gdb_breakpoints(session_id?)` | Manage and list breakpoints |
| `gdb_run(args?, timeout_seconds?, session_id?)` / `gdb_continue(...)` | Run or resume, waiting for the next stop |
| `gdb_step` / `gdb_next` / `gdb_finish` / `gdb_step_instruction` | Source and instruction stepping |
| `gdb_interrupt(session_id?)` | Stop a running program and say where it stopped |
| `gdb_stack(limit?, session_id?)` | Call stack, innermost frame first |
| `gdb_variables(frame?, session_id?)` | Variables in a frame |
| `gdb_evaluate(expression, session_id?)` | Evaluate an expression in the current frame |
| `gdb_registers(session_id?)` | General-purpose registers as hex |
| `gdb_read_memory(address, count?, session_id?)` | Raw bytes at an address or symbol |
| `gdb_command(command, session_id?)` | Any console command, e.g. `info threads` |
| `gdb_status(session_id?)` | One session's state, or all of them |

Every tool answers `{"ok": true, ...}` or `{"ok": false, "error": "...", "error_type": "..."}`,
so a client can react to failures instead of parsing exceptions. Stop reasons are
normalised (`breakpoint`, `step`, `finished`, `signal`, `exited`, `watchpoint`),
so a client can branch on them without knowing GDB's spelling.

## Test

```sh
.venv/bin/python -u tests/test_mcp.py
```

The test builds three small programs, launches the server as a subprocess over
stdio, and walks the whole surface. 46 checks: tool listing, `~/.gdbinit`
loading, breakpoints, run, variables, evaluation, stepping, the call stack,
memory, registers, a console command, deleting a breakpoint, finish, an
intentional error, two sessions side by side with isolated state, an ambiguous
call that must be refused, attaching to a live PID, generating and opening a core
dump, detaching without killing the target, teardown, and calls that must fail
cleanly afterwards. It fails the run if any tool answers `ok=false` when it
should not.

## Limits

- One GDB per session, local targets only; remote targets go through
  `gdb_command("target remote host:port")`.
- `attach` needs ptrace permission: on a host with `kernel.yama.ptrace_scope=1`
  the target must be a descendant of the server, or set
  `PR_SET_PTRACER` in the target (the test's `tests/demo_loop.c` does this).
- `gdb_stack` and `gdb_variables` report the selected frame; use
  `gdb_variables(frame)` or `gdb_command("frame N")` to switch.
- Pretty-printing is on, so values arrive formatted rather than raw.
- No authentication and no sandboxing: a debugger can run shell commands, so only
  debug code you trust.
