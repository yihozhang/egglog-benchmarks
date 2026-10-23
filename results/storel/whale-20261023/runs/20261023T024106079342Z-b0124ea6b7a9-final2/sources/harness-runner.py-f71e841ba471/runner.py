"""Building systems and running the benchmark matrix of an experiment."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import signal
import functools
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import artifacts
from .config import (ROOT, Benchmark, Experiment, Variant, is_patch_path, load_system,
                     patch_path, resolve_benchmarks, validate_env, validate_settings)

# Exit code an adapter uses to say "this system/ref cannot run this configuration".
UNSUPPORTED_EXIT = 3


@dataclass(frozen=True)
class Build:
    dir: Path
    info: dict  # contents of <dir>/build.json, written by the adapter


def build(system: str, ref: str, patches: tuple[str, ...] = ()) -> Build:
    """Idempotently build `system` at git ref `ref` (+ patches); the adapter prints the build dir."""
    sysd = load_system(system).dir
    patches = tuple(str(patch_path(system, p)) if is_patch_path(p) else p for p in patches)
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
    cores = set()
    sockets = set()
    try:
        for block in Path("/proc/cpuinfo").read_text().split("\n\n"):
            fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
            fields = {k.strip(): v.strip() for k, v in fields.items()}
            model = model or fields.get("model name", "")
            if "physical id" in fields:
                sockets.add(fields["physical id"])
                if "core id" in fields:
                    cores.add((fields["physical id"], fields["core id"]))
    except OSError:
        pass
    host = {
        "hostname": platform.node(),
        "cpu": model,
        "ncpu": os.cpu_count(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "physical_cores": len(cores) or None,
        "sockets": len(sockets) or None,
    }
    if hasattr(os, "sched_getaffinity"):
        host["cpu_affinity"] = sorted(os.sched_getaffinity(0))
    try:
        total = next(line for line in Path("/proc/meminfo").read_text().splitlines()
                     if line.startswith("MemTotal:"))
        host["memory_bytes"] = int(total.split()[1]) * 1024
    except (OSError, StopIteration, ValueError):
        pass
    try:
        host["numa_nodes"] = {
            p.parent.name: p.read_text().strip()
            for p in Path("/sys/devices/system/node").glob("node*/cpulist")
        }
        host["cpu_governors"] = sorted({p.read_text().strip() for p in
            Path("/sys/devices/system/cpu/cpufreq").glob("policy*/scaling_governor")})
    except OSError:
        pass
    return host


def repo_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        return ""


def measurement_scope(variant: Variant, bench: Benchmark) -> str:
    return bench.egglog_timing if variant.system == "egglog" else "process"


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
    env = {**os.environ, **variant.env, "BENCH_TIMEOUT_S": str(int(timeout)), "BENCH_MEMORY_GB": str(memory_gb)}
    (log_dir / "env.json").write_text(json.dumps(variant.env, indent=2) + "\n")
    if variant.system == "egglog":
        env["BENCH_EGGLOG_TIMING"] = bench.egglog_timing
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
    try:
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
    except BaseException:
        # The child owns a process group; interruption must not leave a benchmark
        # running while the bundle says it was interrupted.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            _, child_status, _ = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(child_status)
        except ChildProcessError:
            pass
        raise
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
    if status == "fail" and report.get("status_hint") == "timeout":
        status = "timeout"
    if status == "fail" and (report.get("status_hint") == "oom" or (memory_gb and rc == -signal.SIGTERM)):
        # The adapter saw the OOM kill, or systemd stopped the whole scope (SIGTERM) after one.
        status = "oom"
    if status == "ok" and measurement_scope(variant, bench) == "analysis" and (
        report.get("timing_scope") != "analysis" or "wall_s" not in report
    ):
        status = "fail"
        report["measurement_error"] = "analysis timing requested but adapter did not report analysis wall time"
    return {
        "status": status,
        "exit_code": rc,
        # wall_s is the adapter's measured interval; timing_scope identifies whether
        # it excludes setup. harness_wall_s and resource usage cover the full process.
        "wall_s": report.get("wall_s", wall),
        "timing_scope": report.get("timing_scope", measurement_scope(variant, bench)),
        "harness_wall_s": wall,
        "user_s": ru.ru_utime,
        "sys_s": ru.ru_stime,
        "max_rss_kb": ru.ru_maxrss,
        "report": report,
        "log_dir": str(log_dir),  # made bundle-relative by run_experiment
    }


def identity(value) -> str:
    """A deterministic identity for JSON-compatible measurement settings."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def benchmark_identity(bench: Benchmark, systems: dict, *, rd: Path | None = None) -> dict:
    """Identify the input, included rules, suite definition, facts, and ports."""
    files = {}
    visited = set()

    def visit(path: Path):
        path = path.resolve()
        if path in visited:
            return
        visited.add(path)
        files[os.path.relpath(path, bench.dir)] = artifacts.file_identity(path)
        if path.suffix == ".egg":
            source = re.sub(r';[^\n]*|"(?:[^"\\]|\\.)*"',
                            lambda m: "" if m.group().startswith(";") else m.group(), path.read_text())
            for quoted in re.findall(r'\(\s*include\s+("(?:[^"\\]|\\.)*")\s*\)', source):
                include = Path(json.loads(quoted))
                target = bench.dir / include
                if not target.exists():
                    target = path.parent / include
                visit(target)

    visit(bench.file)
    suite_file = bench.dir / "suite.toml"
    core = {
        "program": os.path.relpath(bench.file, bench.dir),
        "files": files,
        "suite": artifacts.file_identity(suite_file) if suite_file.exists() else None,
        "facts": artifacts.tree_identity(bench.fact_dir) if bench.fact_dir else None,
        "egglog_timing": bench.egglog_timing,
    }
    source_files = {f"inputs/{bench.id}/{name}": bench.dir / name for name in files}
    if suite_file.exists():
        source_files[f"inputs/{bench.id}/suite.toml"] = suite_file
    ports = {}
    for name, system in systems.items():
        if system.port_dir:
            path = bench.dir / system.port_dir
            ports[name] = artifacts.tree_identity(path) if path.exists() else None
            source_suffixes = {".rs", ".egg", ".py", ".toml", ".in", ".sh", ".sql", ".dl",
                               ".lisp", ".jl", ".ml", ".sbt", ".scala", ".cabal", ".hs", ".properties"}
            for source in path.rglob("*"):
                if source.is_file() and source.suffix in source_suffixes:
                    source_files[f"inputs/{bench.id}/ports/{name}/{source.relative_to(path)}"] = source
    snapshots = artifacts.snapshot_files(rd, source_files) if rd is not None else {}
    return {**core, "input_id": identity(core), "ports": ports, "source_snapshots": snapshots}


def executable_identities(system: str, build_: Build, benches: list[Benchmark]) -> tuple[dict, dict]:
    """Fingerprint known executable layouts, including targets of symlinks."""
    engine = {}
    ports = {}
    if system == "egglog" and (build_.dir / "egglog").is_file():
        engine = artifacts.file_identity(build_.dir / "egglog")
    if system in {"egg", "oatlog"}:
        for bench in benches:
            directory = build_.dir / "ports" / "target" / "release"
            binary = directory / f"{bench.suite}--{bench.file.stem}"
            if system == "egg" and not binary.is_file():
                match = re.fullmatch(r"(.+)-(\d+)", bench.file.stem)
                if match:
                    binary = directory / f"{bench.suite}--{match.group(1)}"
            if binary.is_file():
                ports[bench.id] = artifacts.file_identity(binary)
    return engine, ports


def run_experiment(
    exp: Experiment,
    *,
    only_benchmarks: list[str] | None = None,
    only_variants: list[str] | None = None,
    threads: list[int] | None = None,
    reps: int | None = None,
    timeout: float | None = None,
    memory_gb: float | None = None,
    cpus: str | None = None,
    machine: str | None = None,
    dry_run: bool = False,
    tag: str | None = None,
) -> Path | None:
    benches = [replace(b, egglog_timing=exp.benchmark_timing.get(b.id, b.egglog_timing))
               for b in resolve_benchmarks(only_benchmarks or exp.benchmarks)]
    # Duplicate selectors must not silently duplicate measurements.
    benches = list({b.id: b for b in benches}.values())
    variants = [exp.variant(l) for l in dict.fromkeys(only_variants)] if only_variants else exp.variants
    effective_threads = threads if threads is not None else exp.threads
    reps = reps if reps is not None else exp.reps
    timeout = timeout if timeout is not None else exp.timeout
    memory_gb = memory_gb if memory_gb is not None else exp.memory_gb
    cpus = (cpus if cpus is not None else exp.cpus) or None
    validate_settings(effective_threads, reps, timeout, memory_gb)
    variant_threads = {
        v.label: list(dict.fromkeys(threads if threads is not None else (v.threads or effective_threads)))
        for v in variants
    }
    for values in variant_threads.values():
        validate_settings(values, reps, timeout, memory_gb)
    if not benches or not variants:
        raise SystemExit("an experiment must select at least one benchmark and variant")
    for v in variants:
        validate_env(v.env, v.label)
    if any(b.egglog_timing not in ("process", "analysis") for b in benches):
        raise SystemExit("benchmark timing must be process or analysis")
    systems = {v.system: load_system(v.system) for v in variants}
    print(f"experiment {exp.name}: {len(benches)} benchmarks x {len(variants)} variants; "
          f"{reps} reps, timeout {timeout:g}s, memory cap {memory_gb:g} GB")
    if dry_run:
        for b in benches:
            for v in variants:
                sup = systems[v.system].supports(b)
                print(f"  {b.id:40s} {v.label:20s} threads={variant_threads[v.label]} "
                      f"{'' if sup else 'UNSUPPORTED'}")
        return None

    effective_config = {
        "description": exp.description,
        "benchmarks": [b.id for b in benches],
        "benchmark_timing": {b.id: b.egglog_timing for b in benches},
        "variants": [{"label": v.label, "system": v.system, "ref": v.ref,
                      "flags": list(v.flags), "patches": list(v.patches), "env": dict(v.env),
                      "threads": variant_threads[v.label]} for v in variants],
        "threads": list(dict.fromkeys(effective_threads)),
        "reps": reps, "timeout": timeout, "memory_gb": memory_gb, "cpus": cpus,
        "plot": exp.plot, "baseline": exp.baseline if exp.baseline in variant_threads else None,
        "metric": exp.metric, "speedup_labels": exp.speedup_labels,
    }
    host = host_info()
    rd, manifest = artifacts.create_run(
        exp.name, "matrix", effective_config, host, machine=machine, tag=tag, source=exp.source_path,
    )
    run_id = manifest["run_id"]
    print(f"  run {run_id}: {rd}")
    state = "failed"
    error = None
    recorded = 0
    terminal_cases = 0
    cases = [{"suite": b.suite, "benchmark": b.name, "variant": v.label,
              "threads": t, "reps": reps, "timing_scope": measurement_scope(v, b)}
             for b in benches for v in variants for t in variant_threads[v.label]]
    manifest["cases"] = cases
    manifest["inputs"] = {}
    manifest["builds"] = {}
    total = len(cases) * reps
    try:
        prefix = memory_cap_prefix(memory_gb) if memory_gb else []
        policy = {
            "timeout": timeout, "memory_gb": memory_gb, "cpus": cpus,
            "memory_cap_method": "systemd" if prefix and Path(prefix[0]).name == "systemd-run"
                                 else "rlimit_as" if prefix else "none",
            "memory_cap_prefix": prefix,
            "schedule": "benchmark,variant,threads,repetition",
            "repetition_policy": "stop_case_on_first_non_ok",
        }
        manifest["measurement_policy"] = policy
        toolchain = manifest.get("provenance", {}).get("toolchain", {})
        environment_id = identity({"machine_id": manifest["machine_id"], "host": host,
                                   "measurement_policy": policy, "toolchain": toolchain})
        manifest["environment_id"] = environment_id
        source_files = {f"harness/{name}": ROOT / "harness" / name
                        for name in ("runner.py", "config.py", "__init__.py")}
        source_files = {name: path for name, path in source_files.items() if path.exists()}
        source_snapshots = artifacts.snapshot_files(rd, source_files)
        harness_identity = {name: entry["sha256"] for name, entry in source_snapshots.items()}
        manifest["measurement_sources"] = source_snapshots
        manifest["inputs"] = {b.id: benchmark_identity(b, systems, rd=rd) for b in benches}
        artifacts.write_manifest(rd, manifest)
        builds = {}
        for v in variants:
            sysd = systems[v.system].dir
            patch_labels = [f"patches/{v.system}/explicit-{index}-{Path(name).name}"
                            if is_patch_path(name) else f"patches/{v.system}/{name}.patch"
                            for index, name in enumerate(v.patches)]
            patches = {label: patch_path(v.system, name)
                       for label, name in zip(patch_labels, v.patches)}
            patch_snapshots = artifacts.snapshot_files(rd, patches)
            patch_hashes = [patch_snapshots[label]["sha256"] for label in patch_labels]
            adapter_files = {
                f"adapters/{v.system}/{path.relative_to(sysd).as_posix()}": path
                for path in sysd.rglob("*")
                if path.is_file() and "patches" not in path.relative_to(sysd).parts
                and "__pycache__" not in path.relative_to(sysd).parts
                and not any(part.startswith(".") for part in path.relative_to(sysd).parts)
            }
            adapters = artifacts.snapshot_files(rd, adapter_files)
            adapter_identity = {name: entry["sha256"] for name, entry in adapters.items()}
            info = {"system": v.system, "ref": v.ref, "flags": list(v.flags), "env": dict(v.env),
                    "patches": list(v.patches), "patch_snapshots": patch_snapshots,
                    "patch_hashes": patch_hashes, "adapter_snapshots": adapters}
            manifest["builds"][v.label] = info
            artifacts.write_manifest(rd, manifest)
            builds[v.label] = build(v.system, v.ref, v.patches)
            for name, path in patches.items():
                if artifacts.file_identity(path)["sha256"] != patch_snapshots[name]["sha256"]:
                    raise RuntimeError(f"patch changed while building: {path}")
            build_info = builds[v.label].info
            info["metadata"] = build_info
            info["commit"] = build_info.get("commit", "")
            engine_binary, port_binaries = executable_identities(v.system, builds[v.label], benches)
            info["engine_executable"] = engine_binary or None
            info["port_executables"] = port_binaries
            info["provenance_scope"] = {
                "metadata": "reported_by_adapter",
                "toolchain": "observed_at_run_creation_not_necessarily_at_build_time",
                "source_snapshots": "current_sources_may_differ_from_cached_build_inputs",
                "executable_hashes": "known_local_layouts_only; container_images_are_adapter_reported",
            }
            info["build_id"] = identity({
                "system": v.system, "commit": info["commit"], "patch_hashes": patch_hashes,
                "flags": list(v.flags), "env": dict(v.env),
                "harness": harness_identity, "adapters": adapter_identity,
                "engine_executable": engine_binary,
            })
            artifacts.write_manifest(rd, manifest)
            print(f"  {v.label}: {v.system}@{v.ref} -> {builds[v.label].dir} "
                  f"({info['commit'][:12]}) patches={list(v.patches)} flags={list(v.flags)} env={v.env}")
        base = {
            "experiment": exp.name, "run_id": run_id, "host": host,
            "harness_commit": manifest.get("provenance", {}).get("harness_commit", repo_commit()),
            "machine_id": manifest["machine_id"], "environment_id": environment_id,
        }
        n = 0
        with (rd / "measurements.jsonl").open("w") as out:
            for b in benches:
                input_info = manifest["inputs"][b.id]
                for v in variants:
                    info = manifest["builds"][v.label]
                    port_id = identity({"source": input_info["ports"].get(v.system),
                                        "executable": info["port_executables"].get(b.id)})
                    for t in variant_threads[v.label]:
                        config_id = identity({
                            "build_id": info["build_id"], "input_id": input_info["input_id"],
                            "port_id": port_id, "threads": t,
                            "measurement_policy": policy, "timing_scope": measurement_scope(v, b),
                        })
                        row_base = {
                            **base, "suite": b.suite, "benchmark": b.name, "variant": v.label,
                            "system": v.system, "ref": v.ref, "commit": info["commit"],
                            "patches": list(v.patches), "patch_hashes": info["patch_hashes"],
                            "flags": list(v.flags), "env": dict(v.env),
                            "timing_scope": measurement_scope(v, b), "threads": t, "config_id": config_id,
                            "build_id": info["build_id"], "input_id": input_info["input_id"],
                            "port_id": port_id,
                        }
                        if not systems[v.system].supports(b):
                            row = {**row_base, "rep": 0, "status": "unsupported",
                                   "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()}
                            out.write(json.dumps(row) + "\n")
                            out.flush()
                            recorded += 1
                            terminal_cases += 1
                            n += reps
                            print(f"[{n}/{total}] {b.id} {v.label} t={t}: unsupported (no port)")
                            continue
                        for rep in range(reps):
                            n += 1
                            # Hash-derived case directories avoid collisions and unsafe label paths.
                            log_dir = rd / "logs" / identity([b.id, v.label, t])[:20] / f"r{rep}"
                            meas = run_one(v, builds[v.label], b, t, timeout, memory_gb, cpus, log_dir)
                            meas["log_dir"] = str(log_dir.relative_to(rd))
                            row = {**row_base, "rep": rep,
                                   "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), **meas}
                            out.write(json.dumps(row) + "\n")
                            out.flush()
                            recorded += 1
                            print(f"[{n}/{total}] {b.id} {v.label} t={t} r={rep}: "
                                  f"{meas['status']} {meas['wall_s']:.3f}s", flush=True)
                            if meas["status"] != "ok":
                                terminal_cases += 1
                                n += reps - rep - 1
                                break
        state = "complete"
    except (KeyboardInterrupt, GeneratorExit) as exc:
        state = "interrupted"
        error = type(exc).__name__
        raise
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        manifest["completion"] = {
            "planned_repetitions": total, "recorded_repetitions": recorded,
            "terminal_cases": terminal_cases,
        }
        artifacts.finish_run(rd, manifest, state, error=error)
    print(f"results saved to {rd}")
    return rd
