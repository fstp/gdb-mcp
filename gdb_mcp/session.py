"""Tool-shaped wrappers around GDB processes driven through MI.

pygdbmi speaks GDB/MI over a pipe; this module turns that into the operations an
MCP client needs: start a session (launch a program, attach to a PID, or open a
core file), set breakpoints, run/continue/step, and inspect stack, variables,
registers and memory.

Design rules:
  * GDB is stateful, so a session survives between tool calls. Several sessions
    can live side by side, each keyed by a ``session_id``, so two targets can be
    debugged at once without one's breakpoints landing in the other.
  * Every call returns plain JSON-able data with the interesting fields pulled
    out of the MI records. Raw MI stays reachable through ``command()``.
  * Execution control waits for the resulting stop event instead of returning as
    soon as the command is accepted: "run" only answers ``^running``, while the
    useful answer is the ``*stopped`` that follows.
  * The user's ``~/.gdbinit`` is loaded by default (set ``load_init=False`` to
    skip it), so GEF, pwndbg and hand-written helpers keep working.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from typing import Any

from pygdbmi.gdbcontroller import GdbController

DEFAULT_TIMEOUT = 10.0

# Stop reasons as GDB spells them, mapped to the words a client should branch on.
_REASONS = {
    "breakpoint-hit": "breakpoint",
    "exited-normally": "exited",
    "exited-signalled": "exited",
    "signal-received": "signal",
    "end-stepping-range": "step",
    "function-finished": "finished",
    "location-reached": "location",
    "watchpoint-trigger": "watchpoint",
    "read-watchpoint-trigger": "watchpoint",
    "access-watchpoint-trigger": "watchpoint",
}


class GdbError(RuntimeError):
    """Raised when GDB is not running or answers with an error record."""


@dataclass
class Stop:
    """The most recent stop, distilled from a ``*stopped`` record."""

    reason: str = ""
    function: str = ""
    file: str = ""
    line: int | None = None
    address: str = ""
    thread: str = ""
    signal: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"reason": self.reason}
        if self.function:
            data["function"] = self.function
        if self.file:
            data["file"] = self.file
        if self.line is not None:
            data["line"] = self.line
        if self.address:
            data["address"] = self.address
        if self.thread:
            data["thread"] = self.thread
        if self.signal:
            data["signal"] = self.signal
        return data


def _frame_from_payload(payload: dict[str, Any]) -> Stop:
    frame = payload.get("frame") or {}
    line = frame.get("line")
    raw_reason = str(payload.get("reason", ""))
    return Stop(
        reason=_REASONS.get(raw_reason, raw_reason),
        function=str(frame.get("func", "")),
        file=str(frame.get("file", "")),
        line=int(line) if isinstance(line, (int, str)) and str(line).isdigit() else None,
        address=str(frame.get("addr", "")),
        thread=str(payload.get("thread-id", "")),
        signal=str(payload.get("signal-name", "")),
        raw=payload,
    )


class GdbSession:
    """One GDB subprocess, addressed by its own ``session_id``."""

    def __init__(self, session_id: str, load_init: bool = True) -> None:
        self.session_id = session_id
        self.load_init = load_init
        self._lock = threading.Lock()
        self._ctrl: GdbController | None = None
        self._program: str | None = None
        self._pid: int | None = None
        self._core: str | None = None
        self._breakpoints: dict[str, str] = {}
        self.last_stop: Stop | None = None
        self.running = False

    # ---------------------------------------------------------------- lifecycle

    @property
    def active(self) -> bool:
        return self._ctrl is not None

    @property
    def kind(self) -> str:
        if self._core:
            return "core"
        if self._pid is not None:
            return "attached"
        return "launched"

    def start(self, program: str | None = None, args: list[str] | None = None, gdb_path: str = "gdb") -> dict[str, Any]:
        with self._lock:
            if self._ctrl is None:
                self._spawn_locked(gdb_path)
            if args:
                self._send_locked("-exec-arguments " + " ".join(_quote(a) for a in args), timeout=2.0)
            if program:
                self._load_locked(program)
            return self.status_locked()

    def attach(self, pid: int, gdb_path: str = "gdb") -> dict[str, Any]:
        """Attach to a running process. It keeps running after gdb_detach."""
        with self._lock:
            if self._ctrl is None:
                self._spawn_locked(gdb_path)
            self._core = None
            self._send_locked(f"-target-attach {int(pid)}", timeout=10.0)
            self._drain_locked(0.3)
            self._pid = int(pid)
            return self.status_locked()

    def load_core(self, core_path: str, program: str | None = None, gdb_path: str = "gdb") -> dict[str, Any]:
        """Open a core file, optionally with the executable that produced it."""
        with self._lock:
            if self._ctrl is None:
                self._spawn_locked(gdb_path)
            if program:
                self._load_locked(program)
            self._send_locked(f'-interpreter-exec console {_quote(f"core-file {core_path}")}', timeout=30.0)
            self._drain_locked(0.3)
            self._core = core_path
            self._pid = None
            return self.status_locked()

    def detach(self) -> dict[str, Any]:
        with self._lock:
            self._require_locked()
            if self._pid is None:
                raise GdbError("this session is not attached to a process")
            self._send_locked("-target-detach", timeout=10.0)
            self._pid = None
            return self.status_locked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._ctrl is None:
                return {"active": False, "session_id": self.session_id}
            try:
                self._send_locked("-gdb-exit", timeout=2.0)
            except GdbError:
                pass
            try:
                self._ctrl.exit()
            except Exception:  # pragma: no cover - best effort teardown
                pass
            self._ctrl = None
            self._program = None
            self._pid = None
            self._core = None
            self._breakpoints.clear()
            self.last_stop = None
            self.running = False
            return {"active": False, "session_id": self.session_id}

    def _spawn_locked(self, gdb_path: str) -> None:
        resolved = shutil.which(gdb_path) or gdb_path
        command = [resolved]
        if not self.load_init:
            # -nx is what suppresses ~/.gdbinit (and the system gdbinit).
            command.append("-nx")
        command += ["--interpreter=mi3", "-q"]
        self._ctrl = GdbController(command=command)
        self._drain_locked(0.6)
        self._send_locked("-enable-pretty-printing", timeout=2.0)
        # Breakpoints set before the library is loaded should still fire, and a
        # long-running program should stop on a signal rather than vanishing.
        self._send_locked("-gdb-set breakpoint pending on", timeout=2.0)

    def _load_locked(self, program: str) -> None:
        self._require_locked()
        self._send_locked(f"-file-exec-and-symbols {_quote(program)}", timeout=30.0)
        self._program = program
        self._core = None

    # ------------------------------------------------------------------ plumbing

    def _require_locked(self) -> None:
        if self._ctrl is None:
            raise GdbError(
                f"session {self.session_id} has no GDB process yet. "
                "Call gdb_start, gdb_attach or gdb_load_core first."
            )

    def _send_locked(self, command: str, timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
        self._require_locked()
        assert self._ctrl is not None
        responses = self._ctrl.write(command, timeout_sec=min(timeout, 60.0))
        # Raw, not compacted: callers read payload["stack"], ["variables"],
        # ["memory"] and ["BreakpointTable"].
        records = [r for r in responses if isinstance(r, dict)]
        self._absorb_locked(responses)
        for record in records:
            if record.get("type") == "result" and record.get("message") == "error":
                raise GdbError(_error_text(record) or f"GDB rejected {command!r}")
        return records

    def _drain_locked(self, wait: float) -> list[dict[str, Any]]:
        self._require_locked()
        assert self._ctrl is not None
        raw = self._ctrl.get_gdb_response(timeout_sec=wait, raise_error_on_timeout=False)
        self._absorb_locked(raw)
        return [r for r in raw if isinstance(r, dict)]

    def _absorb_locked(self, responses: list[dict[str, Any]]) -> None:
        """Track running/stopped state and breakpoint ids from async records."""
        for record in responses:
            if not isinstance(record, dict):
                continue
            message = str(record.get("message", ""))
            if message == "running":
                self.running = True
            elif message == "stopped":
                payload = record.get("payload") or {}
                if isinstance(payload, dict):
                    self.last_stop = _frame_from_payload(payload)
                self.running = False
            elif "breakpoint" in message and isinstance(record.get("payload"), dict):
                bp = record["payload"].get("bkpt") or {}
                if isinstance(bp, dict) and "number" in bp:
                    self._breakpoints[str(bp["number"])] = str(bp.get("addr", ""))

    def _wait_for_stop_locked(self, timeout: float) -> None:
        """Poll until the inferior stops, or give up after ``timeout``."""
        waited = 0.0
        while waited < timeout:
            self._drain_locked(0.25)
            if not self.running:
                return
            waited += 0.25
        if not self.running:
            return
        # Still running: ask once, wait briefly, then report honestly. (An
        # earlier version recursed here and could loop forever when GDB ignored
        # the interrupt.)
        try:
            self._send_locked("-exec-interrupt", timeout=2.0)
        except GdbError:
            return
        waited = 0.0
        while waited < 2.0:
            self._drain_locked(0.25)
            if not self.running:
                return
            waited += 0.25

    # ----------------------------------------------------------------- execution

    def run(self, args: list[str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        with self._lock:
            self._require_locked()
            if args is not None:
                self._send_locked("-exec-arguments " + " ".join(_quote(a) for a in args), timeout=2.0)
            self.last_stop = None
            self._send_locked("-exec-run", timeout=5.0)
            self._wait_for_stop_locked(timeout)
            return self._stopped_state_locked()

    def resume(self, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        with self._lock:
            self._require_locked()
            self.last_stop = None
            self._send_locked("-exec-continue", timeout=5.0)
            self._wait_for_stop_locked(timeout)
            return self._stopped_state_locked()

    def step(self, kind: str = "step", timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        with self._lock:
            self._require_locked()
            commands = {
                "step": "-exec-step",
                "next": "-exec-next",
                "finish": "-exec-finish",
                "stepi": "-exec-step-instruction",
                "nexti": "-exec-next-instruction",
            }
            if kind not in commands:
                raise GdbError(f"unknown step kind {kind!r}; use one of {sorted(commands)}")
            self.last_stop = None
            self._send_locked(commands[kind], timeout=5.0)
            self._wait_for_stop_locked(timeout)
            return self._stopped_state_locked()

    def interrupt(self) -> dict[str, Any]:
        with self._lock:
            self._require_locked()
            self._send_locked("-exec-interrupt", timeout=2.0)
            self._wait_for_stop_locked(2.0)
            return self._stopped_state_locked()

    # -------------------------------------------------------------- breakpoints

    def breakpoint(self, location: str) -> dict[str, Any]:
        with self._lock:
            self._require_locked()
            records = self._send_locked(f"-break-insert {_quote(location)}", timeout=5.0)
            payload: dict[str, Any] = {}
            for record in records:
                if record.get("type") == "result" and record.get("message") == "done":
                    payload = record.get("payload") or {}
            bkpt = payload.get("bkpt") or {}
            return {
                "number": str(bkpt.get("number", "")),
                "location": _describe_location(bkpt),
                "address": str(bkpt.get("addr", "")),
                "pending": str(bkpt.get("pending", "")) or None,
            }

    def delete_breakpoint(self, number: str) -> dict[str, Any]:
        with self._lock:
            self._require_locked()
            self._send_locked(f"-break-delete {number}", timeout=5.0)
            self._breakpoints.pop(str(number), None)
            return {"deleted": str(number)}

    def breakpoints(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._breakpoints_locked()

    def _breakpoints_locked(self) -> list[dict[str, Any]]:
        self._require_locked()
        records = self._send_locked("-break-list", timeout=5.0)
        out: list[dict[str, Any]] = []
        for record in records:
            payload = record.get("payload") or {}
            table = payload.get("BreakpointTable") or {}
            for item in table.get("body") or []:
                # Flat rows in MI3; MI2 nests them under "bkpt".
                bkpt = (item.get("bkpt", item)) if isinstance(item, dict) else {}
                if bkpt:
                    out.append({
                        "number": str(bkpt.get("number", "")),
                        "location": _describe_location(bkpt),
                        "address": str(bkpt.get("addr", "")),
                        "enabled": str(bkpt.get("enabled", "y")),
                    })
        return out

    # --------------------------------------------------------------- inspection

    def stack(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return self._stack_locked(limit)

    def _stack_locked(self, limit: int = 20) -> list[dict[str, Any]]:
        self._require_locked()
        records = self._send_locked(f"-stack-list-frames 0 {max(0, limit - 1)}", timeout=5.0)
        frames: list[dict[str, Any]] = []
        for record in records:
            payload = record.get("payload") or {}
            for entry in payload.get("stack") or []:
                frame = entry.get("frame", entry) if isinstance(entry, dict) else {}
                level = frame.get("level")
                frames.append({
                    "level": int(level) if isinstance(level, (int, str)) and str(level).isdigit() else level,
                    "function": frame.get("func", ""),
                    "file": frame.get("file", ""),
                    "line": frame.get("line", ""),
                    "address": frame.get("addr", ""),
                })
        return frames

    def variables(self, frame: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return self._variables_locked(frame)

    def _variables_locked(self, frame: int | None = None) -> list[dict[str, Any]]:
        self._require_locked()
        if frame is not None:
            self._send_locked(f"-stack-select-frame {frame}", timeout=5.0)
        records = self._send_locked("-stack-list-variables --simple-values", timeout=5.0)
        out: list[dict[str, Any]] = []
        for record in records:
            payload = record.get("payload") or {}
            for variable in payload.get("variables") or []:
                if not isinstance(variable, dict):
                    continue
                out.append({
                    "name": variable.get("name", ""),
                    "value": variable.get("value", ""),
                    "type": variable.get("type", ""),
                })
        return out

    def evaluate(self, expression: str) -> dict[str, Any]:
        with self._lock:
            self._require_locked()
            records = self._send_locked(f'-data-evaluate-expression {_quote(expression)}', timeout=5.0)
            value = ""
            for record in records:
                payload = record.get("payload") or {}
                if "value" in payload:
                    value = str(payload["value"])
            return {"expression": expression, "value": value}

    def registers(self, names: bool = True) -> dict[str, Any]:
        with self._lock:
            self._require_locked()
            records = self._send_locked("-data-list-register-names", timeout=5.0)
            register_names: list[str] = []
            for record in records:
                payload = record.get("payload") or {}
                register_names = list(payload.get("register-names") or [])
            values = self._send_locked("-data-list-register-values x", timeout=5.0)
            register_values: list[dict[str, Any]] = []
            for record in values:
                payload = record.get("payload") or {}
                for item in payload.get("register-values") or []:
                    if not isinstance(item, dict):
                        continue
                    number = item.get("number", 0)
                    try:
                        index = int(number)
                    except (TypeError, ValueError):
                        index = None
                    name = ""
                    if names and index is not None and index < len(register_names):
                        name = register_names[index]
                    if name:
                        register_values.append({"name": name, "value": item.get("value", "")})
            return {"count": len(register_values), "registers": register_values}

    def memory(self, address: str, count: int = 16) -> dict[str, Any]:
        with self._lock:
            self._require_locked()
            records = self._send_locked(f"-data-read-memory-bytes {address} {max(1, count)}", timeout=5.0)
            chunks: list[str] = []
            for record in records:
                payload = record.get("payload") or {}
                for entry in payload.get("memory") or []:
                    if isinstance(entry, dict):
                        chunks.append(str(entry.get("contents", "")))
            return {"address": address, "bytes": count, "hex": "".join(chunks)}

    # ---------------------------------------------------------------------- misc

    def command(self, cli: str) -> dict[str, Any]:
        """Escape hatch: run a GDB console command and return its console output."""
        with self._lock:
            self._require_locked()
            records = self._send_locked(f'-interpreter-exec console {_quote(cli)}', timeout=15.0)
            text = "".join(str(r.get("payload") or "") for r in records if r.get("type") == "console")
            return {"command": cli, "output": text.strip()}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self.status_locked()

    def status_locked(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "session_id": self.session_id,
            "active": self._ctrl is not None,
            "kind": self.kind if self._ctrl else None,
            "load_init": self.load_init,
            "program": self._program,
            "pid": self._pid,
            "core": self._core,
            "running": self.running,
            "breakpoints": sorted(self._breakpoints, key=lambda n: int(n) if n.isdigit() else 0),
            "last_stop": self.last_stop.as_dict() if self.last_stop else None,
        }
        return base

    def _stopped_state_locked(self) -> dict[str, Any]:
        state = self.status_locked()
        if self.last_stop is None:
            state["note"] = "no stop event within the timeout (still running, or exited)"
            return state
        # Lock-free internals: the caller already holds the lock.
        try:
            state["variables"] = self._variables_locked()
        except GdbError:
            state["variables"] = []
        try:
            state["stack"] = self._stack_locked(limit=5)
        except GdbError:
            state["stack"] = []
        return state


class SessionManager:
    """Owns every live GDB session, addressed by ``session_id``."""

    def __init__(self) -> None:
        self._sessions: dict[str, GdbSession] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def create(self, program: str | None = None, args: list[str] | None = None,
               load_init: bool = True, gdb_path: str = "gdb") -> GdbSession:
        with self._lock:
            self._counter += 1
            session = GdbSession(session_id=str(self._counter), load_init=load_init)
            self._sessions[session.session_id] = session
        session.start(program=program, args=args, gdb_path=gdb_path)
        return session

    def attach(self, pid: int, load_init: bool = True, gdb_path: str = "gdb") -> GdbSession:
        with self._lock:
            self._counter += 1
            session = GdbSession(session_id=str(self._counter), load_init=load_init)
            self._sessions[session.session_id] = session
        session.attach(pid, gdb_path=gdb_path)
        return session

    def load_core(self, core_path: str, program: str | None = None,
                  load_init: bool = True, gdb_path: str = "gdb") -> GdbSession:
        with self._lock:
            self._counter += 1
            session = GdbSession(session_id=str(self._counter), load_init=load_init)
            self._sessions[session.session_id] = session
        session.load_core(core_path, program=program, gdb_path=gdb_path)
        return session

    def resolve(self, session_id: str | None) -> GdbSession:
        """Find a session, defaulting to the only one when the id is omitted."""
        with self._lock:
            live = {sid: s for sid, s in self._sessions.items() if s.active}
            if session_id is None:
                if len(live) == 1:
                    return next(iter(live.values()))
                if not live:
                    raise GdbError("no session is open. Call gdb_start, gdb_attach or gdb_load_core first.")
                raise GdbError(
                    "several sessions are open, so session_id is required: "
                    + ", ".join(sorted(live, key=int))
                )
            session = self._sessions.get(str(session_id))
            if session is None:
                raise GdbError(f"unknown session_id {session_id!r}; open sessions: {sorted(live) or 'none'}")
            if not session.active:
                raise GdbError(f"session {session_id} has been closed")
            return session

    def close(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is None:
            with self._lock:
                live = [s for s in self._sessions.values() if s.active]
            if len(live) != 1:
                raise GdbError(
                    "session_id is required when several sessions are open: "
                    + ", ".join(sorted(s.session_id for s in live)) if live else "no session is open"
                )
            session_id = live[0].session_id
        with self._lock:
            session = self._sessions.pop(str(session_id), None)
        if session is None:
            raise GdbError(f"unknown session_id {session_id!r}")
        return session.stop()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [s.status() for s in sessions]

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.stop()
            except Exception:  # pragma: no cover - teardown best effort
                pass


def _quote(value: str) -> str:
    """Quote an argument for MI (double quotes, backslash escapes)."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _describe_location(bkpt: dict[str, Any]) -> str:
    file = bkpt.get("file", "")
    line = bkpt.get("line", "")
    func = bkpt.get("func", "")
    if file and line:
        return f"{file}:{line}"
    if func:
        return str(func)
    return str(bkpt.get("fullname") or bkpt.get("at") or "")


def _error_text(record: dict[str, Any]) -> str:
    """Pull a readable message out of an error result record."""
    payload = record.get("payload")
    if isinstance(payload, dict):
        return str(payload.get("msg") or payload.get("message") or payload)
    return str(payload or "")
