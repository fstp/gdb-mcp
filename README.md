# gdb-mcp

An MCP server that lets an MCP client (Claude Desktop, Hermes, an editor agent)
drive GDB: start a session, load a binary, set breakpoints, run and step, then
look at the stack, variables, registers and memory.

Built on [pygdbmi](https://github.com/cs01/pygdbmi), which speaks GDB/MI over a
pipe, and the official `mcp` Python SDK.

## Why

Debugging from an agent is a loop of "run it, look at the frame, print the
variable". Every step of that loop is a text command to GDB, so it maps onto
tools cleanly - as long as execution control waits for the actual stop event
instead of returning as soon as GDB accepts `run` (that is the one subtle bit
here: `-exec-run` answers `^running`, and the interesting answer is the
`*stopped` record that arrives afterwards).

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

## Tools

| Tool | What it does |
| --- | --- |
| `gdb_start(program?, args?)` | Start the session, optionally loading a binary |
| `gdb_load(program)` | Load another executable or core file |
| `gdb_breakpoint(location)` | Break on a function, `file:line` or `*address` |
| `gdb_delete_breakpoint(number)` / `gdb_breakpoints()` | Manage and list breakpoints |
| `gdb_run(args?)` / `gdb_continue()` | Run or resume, waiting for the next stop |
| `gdb_step()` / `gdb_next()` / `gdb_finish()` / `gdb_step_instruction()` | Source and instruction stepping |
| `gdb_interrupt()` | Stop a running program and say where it stopped |
| `gdb_stack(limit?)` | Call stack, innermost frame first |
| `gdb_variables(frame?)` | Variables in a frame |
| `gdb_evaluate(expression)` | Evaluate an expression in the current frame |
| `gdb_registers()` | General-purpose registers as hex |
| `gdb_read_memory(address, count)` | Raw bytes at an address or symbol |
| `gdb_command(command)` | Any console command, e.g. `info threads` |
| `gdb_status()` / `gdb_stop()` | Session state, and teardown |

Every tool answers `{"ok": true, ...}` or `{"ok": false, "error": "..."}`, so a
client can react to failures instead of parsing exceptions.

## Test

```sh
.venv/bin/python tests/test_mcp.py
```

The test builds `tests/demo.c`, launches the server as a subprocess over stdio,
and runs the whole surface: breakpoint, run, variables, evaluate, next,
continue, stack, memory, registers, a console command, deleting a breakpoint,
finish, an intentional error, and teardown. It fails the run if any tool answers
`ok=false`.

## Limits

- One GDB session per server process; no multi-inferior or remote-target support
  beyond `gdb_command` ("target remote ...").
- `gdb_stack`/`gdb_variables` report the selected frame; use `gdb_variables(frame)`
  to switch, or `gdb_command("frame N")`.
- Pretty-printing is enabled, so values arrive formatted rather than raw.
