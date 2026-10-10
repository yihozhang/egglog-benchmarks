"""Building systems and running the benchmark matrix of an experiment."""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import platform
import signal
import functools
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, Benchmark, Experiment, Variant, load_system, resolve_benchmarks

# Exit code an adapter uses to say "this system/ref cannot run this configuration".
UNSUPPORTED_EXIT = 3


@dataclass(frozen=True)
class Build:
    dir: Path
    info: dict  # contents of <dir>/build.json, written by the adapter


def build(system: str, ref: str, patches: tuple[str, ...] = ()) -> Build:
    """Idempotently build `system` at git ref `ref` (+ patches); the adapter prints the build dir."""
    sysd = load_system(system).dir
    proc = subprocess.run(
        [str(sysd / "build"), ref, *patches], cwd=ROOT, stdout=subprocess.PIPE, text=True
    )
    if proc.returncode != 0:
        raise SystemExit(f"build of {system}@{ref} {list(patches)} failed (exit {proc.returncode})")
    out = Path(proc.stdout.strip().splitlines()[-1])
    info_path = out / "build.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    return Build(out, info)


@functools.cache
def memory_cap_prefix(gb: float) -> list[str]:
    """Command prefix enforcing a memory cap.

    Prefers a transient systemd user scope (cgroup MemoryMax: accounts resident
    memory, no swap, OOM-kills the offending process); falls back to an address
    space rlimit, which also counts untouched virtual reservations.
    """
    mb = int(gb * 1024)
    if shutil.which("systemd-run"):
        probe = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={mb}M", "--", "true"],
            capture_output=True,
        )
        if probe.returncode == 0:
            # OOMPolicy=continue: the kernel kills only the offending process, so the
            # adapter survives to record the signal instead of the whole scope being stopped.
            return ["systemd-run", "--user", "--scope", "--quiet",
                    "-p", f"MemoryMax={mb}M", "-p", "MemorySwapMax=0", "-p", "OOMPolicy=continue", "--"]
    print(f"warning: systemd-run unavailable, capping memory with RLIMIT_AS={gb}G instead", file=sys.stderr)
    return ["prlimit", f"--as={int(gb * 1024 ** 3)}", "--"]


def host_info() -> dict:
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "hostname": platform.node(),
        "cpu": model,
        "ncpu": os.cpu_count(),
        "kernel": platform.release(),
    }


def repo_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        return ""


def run_one(
    variant: Variant,
    build_: Build,
    bench: Benchmark,
    threads: int,
    timeout: float,
    memory_gb: float,
    cpus: str | None,
    log_dir: Path,
) -> dict:
    """Run one configuration once. Returns the measurement fields of a result row."""
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(load_system(variant.system).dir / "run"),
        str(build_.dir),
        str(bench.file),
        str(bench.fact_dir) if bench.fact_dir else "-",
        str(threads),
        str(log_dir),
        *variant.flags,
    ]
    if cpus:
        cmd = ["taskset", "-c", cpus, *cmd]
    if memory_gb:
        cmd = [*memory_cap_prefix(memory_gb), *cmd]
    # Adapters that cannot be confined by the harness (containers) read the limits from here.
    env = {**os.environ, "BENCH_TIMEOUT_S": str(int(timeout)), "BENCH_MEMORY_GB": str(memory_gb)}
    if cpus:
        env["BENCH_CPUS"] = cpus
    t0 = time.perf_counter()
    with open(log_dir / "harness.stderr", "w") as err:
        proc = subprocess.Popen(
            cmd, cwd=bench.dir, stdout=subprocess.DEVNULL, stderr=err, start_new_session=True, env=env
        )
    # Poll with wait4 so we get this child's own resource usage (including its
    # descendants), not the cumulative RUSAGE_CHILDREN of the harness process.
    status = "ok"
    deadline = t0 + timeout
    while True:
        pid, wstatus, ru = os.wait4(proc.pid, os.WNOHANG)
        if pid != 0:
            break
        if time.perf_counter() > deadline:
            os.killpg(proc.pid, signal.SIGKILL)
            _, wstatus, ru = os.wait4(proc.pid, 0)
            status = "timeout"
            break
        time.sleep(0.005)
    wall = time.perf_counter() - t0
    rc = os.waitstatus_to_exitcode(wstatus)
    proc.returncode = rc  # tell Popen the child is reaped
    if status == "ok":
        if rc == UNSUPPORTED_EXIT:
            status = "unsupported"
        elif rc != 0:
            status = "fail"
    report_path = log_dir / "report.json"
    report = {}
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text())
        except json.JSONDecodeError:
            pass
    if status == "fail" and (report.get("status_hint") == "oom" or (memory_gb and rc == -signal.SIGTERM)):
        # The adapter saw the OOM kill, or systemd stopped the whole scope (SIGTERM) after one.
        status = "oom"
    return {
        "status": status,
        "exit_code": rc,
        # wall_s is the system's own process wall time when the adapter reports it
        # (excludes adapter and cgroup setup); harness_wall_s is measured around everything.
        "wall_s": report.get("wall_s", wall),
        "harness_wall_s": wall,
        "user_s": ru.ru_utime,
        "sys_s": ru.ru_stime,
        "max_rss_kb": ru.ru_maxrss,
        "report": report,
        "log_dir": str(log_dir.relative_to(ROOT)),
    }


def run_experiment(
    exp: Experiment,
    *,
    only_benchmarks: list[str] | None = None,
    only_variants: list[str] | None = None,
    threads: list[int] | None = None,
    reps: int | None = None,
    timeout: float | None = None,
    memory_gb: float | None = None,
    dry_run: bool = False,
    tag: str | None = None,
) -> Path | None:
    benches = resolve_benchmarks(only_benchmarks or exp.benchmarks)
    variants = [exp.variant(l) for l in only_variants] if only_variants else exp.variants
    threads = threads or exp.threads
    reps = reps if reps is not None else exp.reps
    timeout = timeout if timeout is not None else exp.timeout
    memory_gb = memory_gb if memory_gb is not None else exp.memory_gb

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{tag}" if tag else "")
    print(f"experiment {exp.name}: run {run_id}")
    print(f"  {len(benches)} benchmarks x {len(variants)} variants x threads {threads} x {reps} reps; "
          f"timeout {timeout:g}s, memory cap {memory_gb:g} GB")

    if dry_run:
        for b in benches:
            for v in variants:
                sup = load_system(v.system).supports(b)
                print(f"  {b.id:40s} {v.label:20s} {'' if sup else 'UNSUPPORTED'}")
        return None

    builds = {v.label: build(v.system, v.ref, v.patches) for v in variants}
    for v in variants:
        print(f"  {v.label}: {v.system}@{v.ref} -> {builds[v.label].dir.relative_to(ROOT)}"
              f" ({builds[v.label].info.get('commit', '?')[:12]}) patches={list(v.patches)} flags={list(v.flags)}")

    exp.results_dir.mkdir(parents=True, exist_ok=True)
    out_path = exp.results_dir / f"{run_id}.jsonl"
    host = host_info()
    harness_commit = repo_commit()
    base = {
        "experiment": exp.name,
        "run_id": run_id,
        "host": host,
        "harness_commit": harness_commit,
    }
    n_total = len(benches) * len(variants) * len(threads) * reps
    n = 0
    with out_path.open("a") as out:
        # Benchmarks outermost, variants interleaved, so a drifting machine affects
        # all variants of a benchmark alike.
        for b in benches:
            for v, t in itertools.product(variants, threads):
                system = load_system(v.system)
                row_base = {
                    **base,
                    "suite": b.suite,
                    "benchmark": b.name,
                    "variant": v.label,
                    "system": v.system,
                    "ref": v.ref,
                    "commit": builds[v.label].info.get("commit", ""),
                    "patches": list(v.patches),
                    "flags": list(v.flags),
                    "threads": t,
                }
                if not system.supports(b):
                    row = {**row_base, "rep": 0, "status": "unsupported",
                           "timestamp": dt.datetime.now().isoformat(timespec="seconds")}
                    out.write(json.dumps(row) + "\n"); out.flush()
                    n += reps
                    print(f"[{n}/{n_total}] {b.id} {v.label} t={t}: unsupported (no port)")
                    continue
                for rep in range(reps):
                    n += 1
                    log_dir = exp.results_dir / "logs" / run_id / v.label / f"{b.suite}-{b.name}-t{t}-r{rep}"
                    meas = run_one(v, builds[v.label], b, t, timeout, memory_gb, exp.cpus, log_dir)
                    row = {**row_base, "rep": rep,
                           "timestamp": dt.datetime.now().isoformat(timespec="seconds"), **meas}
                    out.write(json.dumps(row) + "\n"); out.flush()
                    print(f"[{n}/{n_total}] {b.id} {v.label} t={t} r={rep}: "
                          f"{meas['status']} {meas['wall_s']:.3f}s", flush=True)
                    if meas["status"] != "ok":
                        # No point repeating a failing or timed-out configuration.
                        n += reps - rep - 1
                        break
    print(f"results appended to {out_path.relative_to(ROOT)}")
    return out_path
