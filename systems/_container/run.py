#!/usr/bin/env python3
"""Generic run adapter for container-based systems (see systems/_container/README.md).

Usage: run <build-dir> <program.egg> <fact-dir|-> <threads> <out-dir> [flags...]
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

UNSUPPORTED = 3
SIZE = re.compile(r"^\s*\(?\((\S+) (\d+)\)\)?\s*$", re.M)
RUN_S = re.compile(r"^run_s\s+([0-9.eE+-]+)\s*$", re.M)
PORT_WALL_S = re.compile(r"^wall_s\s+([0-9.eE+-]+)\s*$", re.M)
WALL_NS = re.compile(r"^wall_ns\s+(\d+)\s*$", re.M)


def resolve_port(port_dir: Path, name: str) -> tuple[str | None, list[str]]:
    """The port for benchmark `name`: a file with stem `name`, or, for `base-<N>`, a file
    with stem `base` that takes `--instance N` (one port serving all instance sizes)."""
    if port_dir.is_dir():
        if any(p.stem == name for p in port_dir.iterdir()):
            return name, []
        m = re.fullmatch(r"(.+)-(\d+)", name)
        if m and any(p.stem == m.group(1) for p in port_dir.iterdir()):
            return m.group(1), ["--instance", m.group(2)]
    return None, []


def main() -> int:
    build_dir, program, fact_dir, threads, out_dir, *flags = sys.argv[1:]
    build_dir, program, out_dir = Path(build_dir), Path(program), Path(out_dir)
    info = json.loads((build_dir / "build.json").read_text())
    out_dir.mkdir(parents=True, exist_ok=True)
    suite, name = program.parent.name, program.stem
    port_dir = build_dir / "context" / "ports" / suite
    port, extra = resolve_port(port_dir, name)
    if port is None:
        print(f"no {info['system']} port for {suite}/{name}", file=sys.stderr)
        return UNSUPPORTED
    threads = int(threads)
    if threads != 1:
        if not info.get("threads"):
            print(f"{info['system']} is single-threaded", file=sys.stderr)
            return UNSUPPORTED
        extra += ["--threads", str(threads)]
    if fact_dir != "-" or any(f not in info.get("flags", []) for f in flags):
        print(f"{info['system']} port: no fact directory, flags {info.get('flags', [])}", file=sys.stderr)
        return UNSUPPORTED
    flags = [*extra, *flags]

    cmd = ["podman", "run", "--rm", "--network=none"]
    if os.environ.get("BENCH_MEMORY_GB", "0") not in ("0", "0.0"):
        cmd += ["--memory", f"{int(float(os.environ['BENCH_MEMORY_GB']) * 1024)}m"]
    if os.environ.get("BENCH_TIMEOUT_S"):
        cmd += ["--timeout", os.environ["BENCH_TIMEOUT_S"]]
    # No --cpuset-cpus: rootless podman on the benchmark host has no cpuset controller
    # delegated, and these engines are single-threaded anyway (BENCH_CPUS is ignored).
    cmd += [info["image"], suite, port, *flags]
    (out_dir / "cmd").write_text(" ".join(cmd) + "\n")

    t0 = time.perf_counter()
    with open(out_dir / "stdout", "w") as so, open(out_dir / "stderr", "w") as se:
        rc = subprocess.call(cmd, stdout=so, stderr=se)
    host_wall = time.perf_counter() - t0

    stdout = (out_dir / "stdout").read_text(errors="replace")
    stderr = (out_dir / "stderr").read_text(errors="replace")
    report = {"commit": info["commit"], "container_wall_s": host_wall}
    # wall_s: the whole port process (entry.sh's wall_ns), unless the port prints its own
    # wall_s, which JIT-language ports do to exclude VM startup and JIT warm-up while still
    # covering e-graph construction, the iterations and the final size count.
    m = WALL_NS.search(stdout)
    if m:
        report["process_wall_s"] = int(m.group(1)) / 1e9
    m = PORT_WALL_S.search(stdout)
    report["wall_s"] = float(m.group(1)) if m else report.get("process_wall_s", host_wall)
    m = RUN_S.search(stdout)
    if m:
        report["run_s"] = float(m.group(1))  # the engine's own number, for reference only
    report["sizes"] = {k: int(v) for k, v in SIZE.findall(stdout)}
    # podman exits 137 when the container's main process was SIGKILLed: by the OOM killer or
    # by --timeout (the harness's own timeout fires at the same time and takes precedence).
    if rc == 137:
        report["status_hint"] = "oom"
    if rc not in (0, 3) and "run_s" not in report:
        print(f"container exited with {rc}: {stderr[-300:]}", file=sys.stderr)
    (out_dir / "report.json").write_text(json.dumps(report, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
