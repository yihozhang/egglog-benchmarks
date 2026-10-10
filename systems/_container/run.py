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
WALL_NS = re.compile(r"^wall_ns\s+(\d+)\s*$", re.M)


def main() -> int:
    build_dir, program, fact_dir, threads, out_dir, *flags = sys.argv[1:]
    build_dir, program, out_dir = Path(build_dir), Path(program), Path(out_dir)
    info = json.loads((build_dir / "build.json").read_text())
    out_dir.mkdir(parents=True, exist_ok=True)
    suite, name = program.parent.name, program.stem
    port_dir = build_dir / "context" / "ports" / suite
    if not port_dir.is_dir() or not any(p.stem == name for p in port_dir.iterdir()):
        print(f"no {info['system']} port for {suite}/{name}", file=sys.stderr)
        return UNSUPPORTED
    if int(threads) != 1 or fact_dir != "-" or any(f not in info.get("flags", []) for f in flags):
        print(f"{info['system']} port: single thread only, no fact directory, flags {info.get('flags', [])}",
              file=sys.stderr)
        return UNSUPPORTED

    cmd = ["podman", "run", "--rm", "--network=none"]
    if os.environ.get("BENCH_MEMORY_GB", "0") not in ("0", "0.0"):
        cmd += ["--memory", f"{int(float(os.environ['BENCH_MEMORY_GB']) * 1024)}m"]
    if os.environ.get("BENCH_TIMEOUT_S"):
        cmd += ["--timeout", os.environ["BENCH_TIMEOUT_S"]]
    if os.environ.get("BENCH_CPUS"):
        cmd += ["--cpuset-cpus", os.environ["BENCH_CPUS"]]
    cmd += [info["image"], suite, name, *flags]
    (out_dir / "cmd").write_text(" ".join(cmd) + "\n")

    t0 = time.perf_counter()
    with open(out_dir / "stdout", "w") as so, open(out_dir / "stderr", "w") as se:
        rc = subprocess.call(cmd, stdout=so, stderr=se)
    host_wall = time.perf_counter() - t0

    stdout = (out_dir / "stdout").read_text(errors="replace")
    stderr = (out_dir / "stderr").read_text(errors="replace")
    report = {"commit": info["commit"], "container_wall_s": host_wall}
    m = WALL_NS.search(stdout)
    report["wall_s"] = int(m.group(1)) / 1e9 if m else host_wall
    m = RUN_S.search(stdout)
    if m:
        report["run_s"] = float(m.group(1))
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
