"""A thin, tool-shaped wrapper around one GDB process driven through MI.

pygdbmi speaks GDB/MI over a pipe; this module turns that into a small set of
operations an MCP client can call: start, load, breakpoints, run/continue/step,
and inspection (stack, variables, registers, memory, expressions).

Design rules:
  * One session per server process. GDB is stateful and an MCP client expects
    its session to survive between tool calls.
  * Every call returns plain JSON-able data with the interesting fields pulled
    out of the MI records. Raw MI is available through ``command()`` for
    anything this wrapper does not model, so nothing is out of reach.
  * Execution control waits for the resulting stop event instead of returning as
    soon as the command is accepted, because "run" only answers ``^running``
    while the useful answer is the ``*stopped`` that follows.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from typing import Any

from pygdbmi.gdbcontroller import GdbController

DEFAULT_TIMEOUT = 10.0


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


_REASONS = {
    "breakpoint-hit": "breakpoint",
    "exited-normally": "exited",
    "exited-signalled": "exited",
    "signal-received": "signal",
    "end-stepping-range": "step",
    "function-finished": "finished",
    "location-reached": "location",
    "watchpoint-trigger": "watchpoint",
}


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
    """Owns exactly one GDB subprocess."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ctrl: GdbController | None = None
        self._program: str | None = None
        self._breakpoints: dict[str, str] = {}
        self.last_stop: Stop | None = None
        self.running = False

    # ---------------------------------------------------------------- lifecycle

    @property
    def active(self) -> bool:
        return self._ctrl is not None

    def start(self, program: str | None = None, args: list[str] | None = None, gdb_path: str = "gdb") -> dict[str, Any]:
        with self._lock:
            if self._ctrl is not None:
                if program:
                    self._load_locked(program)
                return self.status_locked()
            resolved = shutil.which(gdb_path) or gdb_path
            # pygdbmi takes the whole command line, not a path plus args.
            self._ctrl = GdbController(
                command=[resolved, "--interpreter=mi3", "-q", "-nx"],
            )
            # Quiet the startup chatter (the MI banner) so the first tool call
            # does not return a wall of log records.
            self._drain_locked(0.6)
            self._send_locked("-enable-pretty-printing", timeout=2.0)
            if args:
                self._send_locked("-exec-arguments " + " ".join(args), timeout=2.0)
            if program:
                self._load_locked(program)
            return self.status_locked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._ctrl is None:
                return {"active": False}
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
            self._breakpoints.clear()
            self.last_stop = None
            self.running = False
            return {"active": False}

    def _load_locked(self, program: str) -> None:
        self._require_locked()
        self._send_locked(f"-file-exec-and-symbols {_quote(program)}", timeout=30.0)
        self._program = program

    # ------------------------------------------------------------------ plumbing

    def _require_locked(self) -> None:
        if self._ctrl is None:
            raise GdbError("GDB is not running. Call gdb_start first.")

    def _send_locked(self, command: str, timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
        self._require_locked()
        assert self._ctrl is not None
        responses = self._ctrl.write(command, timeout_sec=min(timeout, 30.0))
        # Raw, not compacted: callers read payload["stack"], ["variables"],
        # ["memory"] and ["BreakpointTable"], which _compact would strip.
        records = [r for r in responses if isinstance(r, dict)]
        self._absorb_locked(responses)
        for record in records:
            if record.get("type") == "result" and record.get("message") == "error":
                raise GdbError(record.get("error") or f"GDB rejected {command!r}")
        return records

    def _drain_locked(self, wait: float) -> list[dict[str, Any]]:
        self._require_locked()
        assert self._ctrl is not None
        raw = self._ctrl.get_gdb_response(timeout_sec=wait, raise_error_on_timeout=False)
        self._absorb_locked(raw)
        return [_compact(r) for r in raw if isinstance(r, dict)]

    def _absorb_locked(self, responses: list[dict[str, Any]]) -> None:
        """Track running/stopped state from async records."""
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
        if True:
            self._require_locked()
            records = self._send_locked("-break-list", timeout=5.0)
            out: list[dict[str, Any]] = []
            for record in records:
                payload = record.get("payload") or {}
                table = payload.get("BreakpointTable") or {}
                for item in table.get("body") or []:
                    # Same shape question as the stack: flat in MI3, nested in MI2.
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
        if True:
            self._require_locked()
            records = self._send_locked(f"-stack-list-frames 0 {max(0, limit - 1)}", timeout=5.0)
            frames: list[dict[str, Any]] = []
            for record in records:
                payload = record.get("payload") or {}
                for entry in payload.get("stack") or []:
                    # MI3 returns flat frames here; MI2 nesting (entry["frame"])
                    # still works through the fallback.
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
        if True:
            self._require_locked()
            command = "-stack-list-variables --simple-values"
            if frame is not None:
                command = f"-stack-select-frame {frame}"
                self._send_locked(command, timeout=5.0)
                command = "-stack-list-variables --simple-values"
            records = self._send_locked(command, timeout=5.0)
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
            if self._ctrl is None:
                return {"active": False, "program": None, "running": False, "breakpoints": []}
            return self.status_locked()

    def status_locked(self) -> dict[str, Any]:
        return {
            "active": True,
            "program": self._program,
            "running": self.running,
            "breakpoints": sorted(self._breakpoints, key=lambda n: int(n) if n.isdigit() else 0),
            "last_stop": self.last_stop.as_dict() if self.last_stop else None,
        }

    def _stopped_state_locked(self) -> dict[str, Any]:
        state = self.status_locked()
        if self.last_stop is None:
            state["last_stop"] = None
            state["note"] = "no stop event within the timeout (still running, or exited)"
            return state
        # These run while the caller already holds the lock: calling the public
        # wrappers here deadlocked the whole server on the first gdb_run.
        try:
            state["variables"] = self._variables_locked()
        except GdbError:
            state["variables"] = []
        try:
            state["stack"] = self._stack_locked(limit=5)
        except GdbError:
            state["stack"] = []
        return state


def _quote(value: str) -> str:
    """Quote an argument for MI (double quotes, backslash escapes)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
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


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    """Shrink an MI record to what a caller actually reads."""
    out: dict[str, Any] = {"type": record.get("type", "")}
    if record.get("message") is not None:
        out["message"] = record["message"]
    payload = record.get("payload")
    if isinstance(payload, str):
        out["payload"] = payload.strip()
    elif isinstance(payload, dict):
        keep = {k: v for k, v in payload.items() if k in ("frame", "bkpt", "value", "reason", "thread-id")}
        if keep:
            out["payload"] = keep
    if record.get("type") == "result" and record.get("message") == "error":
        out["error"] = str(payload)
    return out
