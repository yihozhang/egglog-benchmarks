"""Measure a marked egglog schedule using the CLI's completion protocol."""

from __future__ import annotations

import contextlib
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


class ProtocolError(RuntimeError):
    """The engine failed a command or exited before acknowledging it."""


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
        before = "(set-option interactive_mode 0)\n" + before + "\n(set-option interactive_mode 1)\n"
        finish = "(set-option interactive_mode 0)\n" + finish + "\n(set-option interactive_mode 1)\n"
        # Do not allow inherited logging settings to suppress old REPL errors.
        env = {**env, "RUST_LOG": "error", "RUST_LOG_STYLE": "never"}
    phases = [out_dir / name for name in ("setup.egg", "analysis.egg", "finish.egg")]
    for path, source in zip(phases, (before, timed, finish)):
        path.write_text(source)
    commands = [f"(include {json.dumps(str(path.resolve()), ensure_ascii=False)})\n" for path in phases]
    (out_dir / "stdin.egg").write_text("".join(commands))
    report = {"timing_scope": "analysis", "interactive_protocol": protocol,
              "timing_method": "interactive-command"}
    t0 = time.perf_counter()
    deadline = t0 + timeout
    with (out_dir / "stdout").open("wb") as output, (out_dir / "stderr").open("wb") as errors:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE if protocol == "legacy-done" else errors, env=env)
        assert proc.stdin is not None and proc.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
            if proc.stderr is not None:
                selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
            pending_errors = b""

            def request(command: str) -> float:
                nonlocal pending_errors
                proc.stdin.write(command.encode())
                proc.stdin.flush()
                pending = b""
                while True:
                    remaining = deadline - time.perf_counter()
                    ready = selector.select(max(0, remaining))
                    if remaining <= 0 or not ready:
                        raise TimeoutError("egglog interactive command exceeded the run timeout")
                    # Preserve and inspect error output before accepting a completion
                    # when both pipes are ready in the same polling cycle.
                    for key, _ in sorted(ready, key=lambda event: event[0].data != "stderr"):
                        chunk = os.read(key.fd, 65536)
                        received = time.perf_counter()
                        if key.data == "stderr":
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            errors.write(chunk)
                            pending_errors += chunk
                            while b"\n" in pending_errors:
                                line, pending_errors = pending_errors.split(b"\n", 1)
                                if line.startswith(b"[ERROR"):
                                    raise ProtocolError("legacy egglog reported an error; see stderr")
                            continue
                        if not chunk:
                            proc.wait(timeout=max(0.001, deadline - time.perf_counter()))
                            raise ProtocolError("egglog exited before the expected (done) acknowledgment")
                        output.write(chunk)
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            if line.strip() == b"(error)":
                                raise ProtocolError("egglog reported (error); see stderr")
                            if line.strip() == b"(done)":
                                return received

            try:
                setup_end = request(commands[0])
                report["setup_wall_s"] = setup_end - t0
                analysis_start = time.perf_counter()
                analysis_end = request(commands[1])
                report["analysis_wall_s"] = analysis_end - analysis_start
                report["wall_s"] = report["analysis_wall_s"]
                request(commands[2])
                proc.stdin.close()
                rc = proc.wait(timeout=max(0.001, deadline - time.perf_counter()))
            except (ProtocolError, BrokenPipeError, TimeoutError, subprocess.TimeoutExpired) as error:
                errors.write(f"\nAdapter: {error}\n".encode())
                report["protocol_error"] = str(error)
                if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
                    report["status_hint"] = "timeout"
                killed_by_adapter = proc.poll() is None
                if killed_by_adapter:
                    proc.kill()
                child_rc = proc.wait()
                if child_rc == -9 and not killed_by_adapter:
                    report["status_hint"] = "oom"
                report["engine_exit_code"] = child_rc
                if child_rc < 0:
                    report["signal"] = -child_rc
                rc = 124 if report.get("status_hint") == "timeout" else 1
            finally:
                with contextlib.suppress(BrokenPipeError):
                    proc.stdin.close()
                proc.stdout.close()
                if proc.stderr is not None:
                    errors.write(proc.stderr.read())
                    proc.stderr.close()
    report["process_wall_s"] = time.perf_counter() - t0
    report.setdefault("wall_s", report["process_wall_s"])  # failed setup still needs a numeric row
    return rc, report
