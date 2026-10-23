"""Drive egglog through the CLI's completion protocol: analysis timing and node limits."""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import selectors
import subprocess
import time
from pathlib import Path

START = ";; BENCHMARK_TIMING_START"
END = ";; BENCHMARK_TIMING_END"
LEGACY_SCHEDULE = "(run-schedule (saturate (run)))"
NODE_LIMIT = ";; BENCHMARK_NODE_LIMIT"
STALL_STEPS = 5  # unchanged steps beyond the iterations run so far = saturation proxy


class ProtocolError(RuntimeError):
    """The engine failed a command, exited before acknowledging it, or replied unexpectedly."""


def split_program(program: str) -> tuple[str, str, str]:
    """Split on unique, whole-line markers; never infer a boundary from egglog syntax."""
    lines = program.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == START]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError(f"analysis timing requires exactly one ordered pair of {START!r} / {END!r}")
    before = "".join(lines[:starts[0]])
    timed = "".join(lines[starts[0] + 1:ends[0]])
    after = "".join(lines[ends[0] + 1:])
    if not timed.strip():
        raise ValueError("analysis timing interval is empty")
    return before, timed, after


def is_node_limit(line: str) -> bool:
    return line.split()[:2] == NODE_LIMIT.split()


def has_node_limit(program: str) -> bool:
    """Whether the program carries a `;; BENCHMARK_NODE_LIMIT <N> <table>...` marker line."""
    return any(is_node_limit(line) for line in program.splitlines())


def split_node_limit(program: str) -> tuple[str, str, int, list[str]]:
    """Split on the unique marker line into setup (before it) and one step (after it).

    The marker names the limit N and the tables whose `(print-size)` sizes are summed
    against it. The step is the text after the marker, re-sent as a whole per iteration.
    """
    lines = program.splitlines(keepends=True)
    markers = [i for i, line in enumerate(lines) if is_node_limit(line)]
    if len(markers) != 1:
        raise ValueError(f"node-limited iteration requires exactly one {NODE_LIMIT!r} line")
    limit, *tables = lines[markers[0]].split()[2:] or [""]
    if not re.fullmatch(r"[1-9][0-9]*", limit) or not tables or len(set(tables)) != len(tables):
        raise ValueError(f"{NODE_LIMIT} needs a positive limit and distinct table names")
    setup = "".join(lines[:markers[0]])
    step = "".join(lines[markers[0] + 1:])
    if not re.sub(r";[^\n]*", "", step).strip():
        raise ValueError("node-limited iteration step is empty")
    return setup, step, int(limit), tables


NEW_SIZE = re.compile(r"^\s*\(?\((\S+) (\d+)\)\)?\s*$", re.M)  # ((f 12)\n (g 3))
OLD_SIZE = re.compile(r"^(\S+): (\d+)$", re.M)  # f: 12


def parse_sizes(stdout: str) -> dict:
    sizes = {}
    for pat in (NEW_SIZE, OLD_SIZE):
        for name, n in pat.findall(stdout):
            sizes[name] = int(n)
    return sizes


def quiet(source: str) -> str:
    """Legacy egglog acknowledges each desugared command: silence a phase except its end."""
    return "(set-option interactive_mode 0)\n" + source + "\n(set-option interactive_mode 1)\n"


def include(path: Path) -> str:
    return f"(include {json.dumps(str(path.resolve()), ensure_ascii=False)})\n"


class Session:
    """One engine process on a pipe: each request returns when its `(done)` line arrives.

    stdout and stderr go to <out_dir>/stdout and stderr, every command sent to stdin.egg.
    The legacy protocol pipes stderr through the adapter, since its REPL errors can omit
    `(error)`; a `[ERROR` line fails the request. All requests share one deadline.
    """

    FAILURES = (ProtocolError, BrokenPipeError, TimeoutError, subprocess.TimeoutExpired)

    def __init__(self, cmd: list[str], out_dir: Path, env: dict[str, str], deadline: float, protocol: str):
        self.cmd, self.out_dir, self.env, self.deadline, self.protocol = cmd, out_dir, env, deadline, protocol

    def __enter__(self) -> Session:
        with contextlib.ExitStack() as stack:
            self.output = stack.enter_context((self.out_dir / "stdout").open("wb"))
            self.errors = stack.enter_context((self.out_dir / "stderr").open("wb"))
            self.transcript = stack.enter_context((self.out_dir / "stdin.egg").open("w"))
            self.proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE if self.protocol == "legacy-done" else self.errors, env=self.env,
            )
            assert self.proc.stdin is not None and self.proc.stdout is not None
            self.selector = stack.enter_context(selectors.DefaultSelector())
            self.selector.register(self.proc.stdout, selectors.EVENT_READ, "stdout")
            if self.proc.stderr is not None:
                self.selector.register(self.proc.stderr, selectors.EVENT_READ, "stderr")
            self.pending_errors = b""
            self.cleanup = stack.pop_all()
        return self

    def __exit__(self, *exc) -> None:
        with self.cleanup:
            with contextlib.suppress(BrokenPipeError):
                self.proc.stdin.close()
            self.proc.stdout.close()
            if self.proc.stderr is not None:
                self.errors.write(self.proc.stderr.read())
                self.proc.stderr.close()

    def request(self, command: str) -> tuple[float, str]:
        """Send a command; return the time its `(done)` arrived and the output before it."""
        self.transcript.write(command)
        self.transcript.flush()
        self.proc.stdin.write(command.encode())
        self.proc.stdin.flush()
        pending = b""
        reply = []
        while True:
            remaining = self.deadline - time.perf_counter()
            ready = self.selector.select(max(0, remaining))
            if remaining <= 0 or not ready:
                raise TimeoutError("egglog interactive command exceeded the run timeout")
            # Preserve and inspect error output before accepting a completion
            # when both pipes are ready in the same polling cycle.
            for key, _ in sorted(ready, key=lambda event: event[0].data != "stderr"):
                chunk = os.read(key.fd, 65536)
                received = time.perf_counter()
                if key.data == "stderr":
                    if not chunk:
                        self.selector.unregister(key.fileobj)
                        continue
                    self.errors.write(chunk)
                    self.pending_errors += chunk
                    while b"\n" in self.pending_errors:
                        line, self.pending_errors = self.pending_errors.split(b"\n", 1)
                        if line.startswith(b"[ERROR"):
                            raise ProtocolError("legacy egglog reported an error; see stderr")
                    continue
                if not chunk:
                    self.proc.wait(timeout=max(0.001, self.deadline - time.perf_counter()))
                    raise ProtocolError("egglog exited before the expected (done) acknowledgment")
                self.output.write(chunk)
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.strip() == b"(error)":
                        raise ProtocolError("egglog reported (error); see stderr")
                    if line.strip() == b"(done)":
                        return received, b"".join(reply).decode(errors="replace")
                    reply.append(line + b"\n")

    def finish(self) -> int:
        """End the input and wait for the engine to exit; its exit code."""
        self.proc.stdin.close()
        return self.proc.wait(timeout=max(0.001, self.deadline - time.perf_counter()))

    def abort(self, error: Exception, report: dict) -> int:
        """Record a failed request, kill a still-running engine; the adapter's exit code."""
        self.errors.write(f"\nAdapter: {error}\n".encode())
        report["protocol_error"] = str(error)
        if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            report["status_hint"] = "timeout"
        killed_by_adapter = self.proc.poll() is None
        if killed_by_adapter:
            self.proc.kill()
        child_rc = self.proc.wait()
        if child_rc == -9 and not killed_by_adapter:
            report["status_hint"] = "oom"
        report["engine_exit_code"] = child_rc
        if child_rc < 0:
            report["signal"] = -child_rc
        return 124 if report.get("status_hint") == "timeout" else 1


def run_interactive(
    cmd: list[str], program: Path, out_dir: Path, env: dict[str, str], timeout: float,
    protocol: str = "command-done",
) -> tuple[int, dict]:
    """Time a schedule through its native `(done)`, including its final rebuild.

    Current egglog acknowledges an entire include. Legacy egglog acknowledges each
    desugared command; setup/reporting therefore disable intermediate replies and
    re-enable them at the end. The legacy timed block must be exactly the supported
    single saturation schedule, so no extra engine command is inside its timer.
    Old REPL errors can omit `(error)`, so their stderr error logs are also monitored.
    """
    if protocol not in ("command-done", "legacy-done"):
        raise ValueError(f"unknown interactive protocol: {protocol!r}")
    before, timed, after = split_program(program.read_text())
    finish = after + "\n(print-size)\n(print-stats)\n"
    if protocol == "legacy-done":
        # This known syntax desugars to exactly one command in egglog 0.5. Arbitrary
        # commands can emit several done replies and leave a stale phase boundary.
        normalized = re.sub(r"\s+", "", re.sub(r";[^\n]*", "", timed))
        if normalized != re.sub(r"\s+", "", LEGACY_SCHEDULE):
            raise ValueError(f"legacy analysis timing requires exactly {LEGACY_SCHEDULE}")
        before, finish = quiet(before), quiet(finish)
        # Do not allow inherited logging settings to suppress old REPL errors.
        env = {**env, "RUST_LOG": "error", "RUST_LOG_STYLE": "never"}
    phases = [out_dir / name for name in ("setup.egg", "analysis.egg", "finish.egg")]
    for path, source in zip(phases, (before, timed, finish)):
        path.write_text(source)
    report = {"timing_scope": "analysis", "interactive_protocol": protocol,
              "timing_method": "interactive-command"}
    t0 = time.perf_counter()
    with Session(cmd, out_dir, env, t0 + timeout, protocol) as session:
        try:
            setup_end, _ = session.request(include(phases[0]))
            report["setup_wall_s"] = setup_end - t0
            analysis_start = time.perf_counter()
            analysis_end, _ = session.request(include(phases[1]))
            report["analysis_wall_s"] = analysis_end - analysis_start
            report["wall_s"] = report["analysis_wall_s"]
            session.request(include(phases[2]))
            rc = session.finish()
        except Session.FAILURES as error:
            rc = session.abort(error, report)
    report["process_wall_s"] = time.perf_counter() - t0
    report.setdefault("wall_s", report["process_wall_s"])  # failed setup still needs a numeric row
    return rc, report


def run_node_limit(
    cmd: list[str], program: Path, out_dir: Path, env: dict[str, str], timeout: float,
    protocol: str = "command-done",
) -> tuple[int, dict]:
    """Repeat the program's step until its tables hold the marker's e-node count.

    This mirrors egg's `Runner::with_node_limit` for programs that never saturate: the
    setup before `;; BENCHMARK_NODE_LIMIT <N> <table>...` is loaded once, then the step
    after it is re-sent, each time followed by `(print-size)`, until the summed size of
    the listed tables reaches N (stop_reason "node-limit") or the whole `(print-size)`
    reply stayed unchanged for more than (iterations before the quiet period) + STALL_STEPS
    consecutive steps ("stalled"; egglog does not say whether a step changed anything, so
    this is the saturation proxy, and `iterations` then includes those no-op steps). The
    allowance grows with the run because a back-off scheduler can leave every rule banned
    for up to that many steps: a rule banned for 5 * 2^k steps was banned for 5 * (2^k - 1)
    steps before, so no quiet period is longer than the iterations that preceded it plus
    five; egg skips such periods inside its Runner. wall_s covers the whole process, spawn to exit,
    including the per-step `(print-size)` round trips, which cost microseconds to
    milliseconds against iterations that are at least that long. Phases are includes so
    that both protocols acknowledge each exactly once, as in `run_interactive`.
    """
    if protocol not in ("command-done", "legacy-done"):
        raise ValueError(f"unknown interactive protocol: {protocol!r}")
    setup, step, limit, tables = split_node_limit(program.read_text())
    finish = "(print-stats)\n"
    if protocol == "legacy-done":
        setup, step, finish = quiet(setup), quiet(step), quiet(finish)
        env = {**env, "RUST_LOG": "error", "RUST_LOG_STYLE": "never"}
    phases = [out_dir / name for name in ("setup.egg", "step.egg", "finish.egg")]
    for path, source in zip(phases, (setup, step, finish)):
        path.write_text(source)
    report = {"timing_scope": "process", "interactive_protocol": protocol,
              "timing_method": "interactive-node-limit", "node_limit": limit, "node_limit_tables": tables}
    t0 = time.perf_counter()
    with Session(cmd, out_dir, env, t0 + timeout, protocol) as session:
        try:
            session.request(include(phases[0]))
            previous, unchanged = None, 0
            for iteration in itertools.count(1):
                session.request(include(phases[1]))
                _, reply = session.request("(print-size)\n")
                sizes = parse_sizes(reply)
                missing = [table for table in tables if table not in sizes]
                if missing:
                    raise ProtocolError(f"(print-size) does not list {' '.join(missing)}")
                report.update(iterations=iteration, nodes=sum(sizes[table] for table in tables), sizes=sizes)
                unchanged = unchanged + 1 if reply == previous else 0
                previous = reply
                if report["nodes"] >= limit:
                    report["stop_reason"] = "node-limit"
                    break
                if unchanged > (iteration - unchanged) + STALL_STEPS:
                    report["stop_reason"] = "stalled"
                    break
            session.request(include(phases[2]))
            rc = session.finish()
        except Session.FAILURES as error:
            rc = session.abort(error, report)
    report["process_wall_s"] = time.perf_counter() - t0
    report["wall_s"] = report["process_wall_s"]
    return rc, report
