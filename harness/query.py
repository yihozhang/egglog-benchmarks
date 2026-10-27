"""Query-only experiments (experiments/<name>.toml with kind = "query").

`bench run <name>` builds the query driver at `egglog_ref`, and for every program of `suite`
exports the final state, translates every rule, runs every rule in egglog (and with
no-decomp), then runs every query in each SQL engine. Working files (exports, generated
queries) go to build/query/<name>/<run-id>/; results to runs/<run-id>/<program>/.

`bench summary|check|plot <name>` read the latest run (or --run <id>).
"""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tomllib
from pathlib import Path

from . import artifacts
from .config import FIGURES, RESULTS, ROOT, experiment_path
from .runner import host_info

QDIR = ROOT / "systems" / "egglog-query"


def is_query(name: str) -> bool:
    p = experiment_path(name)
    return p.exists() and tomllib.loads(p.read_text()).get("kind") == "query"


def load(name: str) -> dict:
    path = experiment_path(name)
    cfg = tomllib.loads(path.read_text())
    cfg["name"] = path.stem
    return cfg


def programs_of(cfg) -> list[str]:
    sdir = ROOT / "benchmarks" / cfg["suite"]
    return cfg.get("programs") or sorted(p.stem for p in sdir.glob("*.egg") if "header" not in p.stem)


# ------------------------------------------------------------------ run
def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _umbra_identity() -> dict:
    image = "docker.io/umbradb/umbra:latest"
    engine = os.environ.get("CONTAINER_ENGINE") or ("podman" if shutil.which("podman") else "docker")
    try:
        probe = subprocess.run([engine, "image", "inspect", image], capture_output=True, text=True, timeout=10)
        images = json.loads(probe.stdout) if probe.returncode == 0 else []
        if images:
            return {"image": image, "container_engine": engine, "id": images[0].get("Id"),
                    "digests": images[0].get("RepoDigests", [])}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return {"image": image, "container_engine": engine, "identity": "unknown"}


def _positive_int(value, option: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemExit(f"query {option} must be a positive integer")
    return value


def _program_sources(sdir: Path, programs: list[str]) -> dict[str, Path]:
    """Snapshot the selected rule sources and includes, leaving datasets as identities."""
    pending = [sdir / f"{program}.egg" for program in programs]
    seen = set()
    sources = {}
    while pending:
        path = pending.pop().resolve()
        if path in seen:
            continue
        seen.add(path)
        label = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
        sources[f"benchmark/{label}"] = path
        pending.extend(path.parent / name for name in re.findall(r'\(include "([^"\n]+)"\)', path.read_text()))
    if (sdir / "suite.toml").is_file():
        sources["benchmark/suite.toml"] = sdir / "suite.toml"
    return sources


def _validate_program_payload(pdir: Path, engines: list[str], *, no_decomp_only: bool = False) -> None:
    """Require one attempted measurement per translated query and engine/mode."""
    queries = json.loads((pdir / "index.json").read_text())["queries"]
    query_ids = {entry["id"] for entry in queries}
    if len(query_ids) != len(queries):
        raise ValueError(f"duplicate query IDs in {pdir}/index.json")
    for engine in ["egglog", *engines]:
        path = pdir / f"{engine}.jsonl"
        expected = {(query_id, mode) for query_id in query_ids for mode in ([True] if no_decomp_only else [False, True])} if engine == "egglog" else query_ids
        seen = set()
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            key = (record.get("query"), record.get("no_decomp")) if engine == "egglog" else record.get("query")
            if key not in expected or key in seen:
                raise ValueError(f"unexpected or duplicate query record {key!r} in {path}")
            seen.add(key)
        if seen != expected:
            raise ValueError(f"incomplete query payload {path}: missing {len(expected - seen)} measurements")


def run(
    name: str,
    programs: list[str] | None = None,
    engines: list[str] | None = None,
    tag: str | None = None,
    *,
    machine: str | None = None,
    cpus: str | None = None,
    threads: list[int] | int | None = None,
    reps: int | None = None,
    timeout: float | None = None,
    memory_gb: float | None = None,
    dry_run: bool = False,
):
    """Run one query configuration; SQL options do not change egglog's one-pass policy."""
    cfg = load(name)
    source = experiment_path(name)
    experiment = cfg.pop("name")
    overrides = {"programs": programs, "engines": engines, "threads": threads, "reps": reps,
                 "timeout": timeout, "memory_gb": memory_gb, "cpus": cpus, "dry_run": dry_run}
    if cpus is not None or cfg.get("cpus") is not None:
        raise SystemExit("query experiments do not support --cpus: affinity cannot be enforced for all backends")
    if memory_gb is not None or cfg.get("memory_gb") is not None:
        raise SystemExit("query experiments do not support --memory-gb: memory limits cannot be enforced for all backends")
    programs = list(programs if programs is not None else programs_of(cfg))
    engines = list(engines if engines is not None else cfg.get("engines", []))
    if not programs:
        raise SystemExit(f"query experiment {experiment}: no programs selected")
    if len(set(programs)) != len(programs) or len(set(engines)) != len(engines):
        raise SystemExit("query programs and engines must not contain duplicates")
    unknown = set(engines) - {"duckdb", "umbra"}
    if unknown:
        raise SystemExit(f"unsupported query engines: {', '.join(sorted(unknown))}")
    nthreads = threads if threads is not None else cfg.get("threads", 1)
    if isinstance(nthreads, (list, tuple)):
        if len(nthreads) != 1:
            raise SystemExit("query experiments accept exactly one --threads value per run")
        nthreads = nthreads[0]
    nthreads = _positive_int(nthreads, "threads")
    nreps = _positive_int(reps if reps is not None else cfg.get("reps", 3), "reps")
    limit = float(timeout if timeout is not None else cfg.get("timeout", 60))
    if not math.isfinite(limit) or limit <= 0:
        raise SystemExit("query timeout must be positive and finite")
    if "umbra" in engines and nthreads != 1:
        raise SystemExit("query Umbra does not implement thread control; use --threads 1 or select DuckDB only")
    sdir = ROOT / "benchmarks" / cfg["suite"]
    for program in programs:
        if Path(program).name != program or not (sdir / f"{program}.egg").is_file():
            raise SystemExit(f"no query program {cfg['suite']}/{program}")
    cfg.update(programs=programs, engines=engines, threads=nthreads, reps=nreps, timeout=limit)
    measurement = {
        "egglog": {"threads": 1, "reps": 1, "timeout": None},
        "sql": {"threads": nthreads, "reps": nreps, "timeout": limit,
                "scope": "SQL backends only; Umbra uses its server defaults"},
    }
    print(f"query experiment {experiment}: {len(programs)} programs, engines egglog + {engines}")
    print(f"SQL settings: threads={nthreads}, reps={nreps}, timeout={limit:g}s; egglog: 1 thread, 1 pass, no timeout")
    if dry_run:
        print(json.dumps({"experiment": experiment, "config": cfg, "measurement": measurement,
                          "machine": machine, "programs": programs, "engines": engines}, indent=2))
        return None

    out, manifest = artifacts.create_run(experiment, "query", cfg, host_info(), machine=machine, tag=tag, source=source)
    work = ROOT / "build" / "query" / experiment / manifest["run_id"]
    manifest.update(cli_overrides=overrides, programs=programs, engines=engines, measurement=measurement,
                    plan=[{"program": program, "engines":
                           (["egglog-cracked"] if cfg.get("no_decomp_only") else ["egglog", "egglog_nd"]) + engines}
                          for program in programs])
    try:
        manifest["inputs"] = {
            "suite": {"path": str(sdir), **artifacts.tree_identity(sdir)},
            "programs": [{"path": str(sdir / f"{program}.egg"), **artifacts.file_identity(sdir / f"{program}.egg")}
                         for program in programs],
            "source_snapshots": artifacts.snapshot_files(out, _program_sources(sdir, programs)),
        }
        sources = {"recipe": source, "query_harness": Path(__file__),
                   "query_build": QDIR / "build", "run_suite": QDIR / "run_suite.py",
                   "bench_db": QDIR / "bench_db.py", "sqlgen": QDIR / "sqlgen.py"}
        manifest["query_sources"] = artifacts.snapshot_files(out, sources)
        artifacts.write_manifest(out, manifest)
        driver = subprocess.run([str(QDIR / "build"), cfg["egglog_ref"]], capture_output=True,
                                text=True, check=True).stdout.strip().splitlines()[-1]
        commit = subprocess.run(["git", "-C", str(ROOT / "egglog"), "rev-parse", f"{cfg['egglog_ref']}^{{commit}}"],
                                capture_output=True, text=True, check=True).stdout.strip()
        driver_path = Path(driver)
        builds = {"egglog": {"ref": cfg["egglog_ref"], "commit": commit,
                             "driver": {"path": driver, **artifacts.file_identity(driver_path)},
                             "driver_source": artifacts.tree_identity(QDIR / "driver")}}
        build_sources = {}
        for parent in driver_path.parents:
            if (parent / "Cargo.toml").exists():
                for filename in ("Cargo.toml", "Cargo.lock", "src/main.rs"):
                    if (parent / filename).is_file():
                        build_sources[f"driver_{filename.replace('/', '_')}"] = parent / filename
                break
        dependency_info = ROOT / "build" / "egglog" / commit[:12] / "build.json"
        if dependency_info.is_file():
            builds["egglog"]["dependency_build"] = json.loads(dependency_info.read_text())
            build_sources["egglog_build"] = dependency_info
        if build_sources:
            builds["egglog"]["snapshots"] = artifacts.snapshot_files(out, build_sources)
        if "duckdb" in engines:
            builds["duckdb"] = {"version": _package_version("duckdb")}
        if "umbra" in engines:
            builds["umbra"] = {**_umbra_identity(), "client_version": _package_version("psycopg")}
        manifest.update(egglog_commit=commit, driver=driver, builds=builds)
        artifacts.write_manifest(out, manifest)
        env = {**os.environ, "EGGLOG_QUERY_DRIVER": driver, "BENCH_SCRATCH": str(work / "scratch"),
               "EGGLOG_QUERY_NO_DECOMP_ONLY": "1" if cfg.get("no_decomp_only") else "0"}
        if cfg.get("no_decomp_only"):
            env.update(UMBRA_CONTAINER_NAME=f"umbra-paper-{manifest['run_id'][:22]}", UMBRA_PORT="55432")
            manifest["measurement"]["egglog"]["flags"] = ["--no-decomp"]
            manifest["container_settings"] = {key: env[key] for key in ("UMBRA_CONTAINER_NAME", "UMBRA_PORT")}
            artifacts.write_manifest(out, manifest)
        for prog in programs:
            print(f"[{dt.datetime.now():%H:%M:%S}] {prog}: export, translate, egglog", flush=True)
            pdir = work / prog
            (out / prog).mkdir(exist_ok=True)
            try:
                subprocess.run([sys.executable, str(QDIR / "run_suite.py"), cfg["suite"], str(work), prog], env=env, check=True)
                for eng in engines:
                    print(f"[{dt.datetime.now():%H:%M:%S}] {prog}: {eng}", flush=True)
                    subprocess.run([sys.executable, str(QDIR / "bench_db.py"), eng, str(pdir), "--threads", str(nthreads),
                                    "--reps", str(nreps), "--timeout", str(limit)], env=env, check=True)
                    if eng == "umbra":
                        builds["umbra"].update(_umbra_identity())
            finally:
                # Keep completed program payloads even if a later engine fails or is interrupted.
                for filename in ("index.json", "egglog.jsonl", *(f"{engine}.jsonl" for engine in engines)):
                    if (pdir / filename).is_file():
                        shutil.copy(pdir / filename, out / prog / filename)
            for filename in ("index.json", "egglog.jsonl", *(f"{engine}.jsonl" for engine in engines)):
                if not (out / prog / filename).is_file():
                    raise RuntimeError(f"query backend did not produce {prog}/{filename}")
            _validate_program_payload(out / prog, engines, no_decomp_only=cfg.get("no_decomp_only", False))
        artifacts.finish_run(out, manifest, "complete")
    except KeyboardInterrupt:
        artifacts.finish_run(out, manifest, "interrupted", error="KeyboardInterrupt")
        raise
    except BaseException as error:
        artifacts.finish_run(out, manifest, "failed", error=str(error))
        raise
    print(f"results in {out}")
    return out


# ------------------------------------------------------------------ load
def run_dir(name: str, run: str = "latest") -> Path:
    """Resolve a unique query run; incomplete and unknown-state runs require an ID."""
    experiment = Path(name).stem if Path(name).suffix == ".toml" else name
    candidates = list(artifacts.list_runs(experiment=experiment))
    legacy = RESULTS / experiment
    if legacy.is_dir():
        candidates.extend(d for d in legacy.iterdir() if d.is_dir() and (d / "run.json").is_file())
    runs = []
    for directory in dict.fromkeys(candidates):
        if directory.parent == legacy:
            info = json.loads((directory / "run.json").read_text())
        else:
            info = artifacts.read_manifest(directory)
        if info.get("kind", "query") == "query":
            runs.append((directory, info))
    if run in ("latest", "all"):
        complete = [(directory, info) for directory, info in runs if info.get("state") == "complete"]
        if not complete:
            raise SystemExit(f"no complete query runs for {experiment}; select a legacy or incomplete run explicitly with --run <id>")
        return max(complete, key=lambda item: (item[1].get("created_at", item[1].get("started", "")), item[0].name))[0]
    exact = [(directory, info) for directory, info in runs
             if directory.name == run or info.get("run_id") == run]
    matches = exact or [(directory, info) for directory, info in runs
                        if directory.name.startswith(run) or directory.name.endswith("-" + run)
                        or info.get("tag") == run or str(info.get("run_id", "")).startswith(run)]
    if not matches:
        raise SystemExit(f"no run of {name} matches {run!r}")
    if len(matches) != 1:
        raise SystemExit(f"ambiguous query run {run!r}: {', '.join(directory.name for directory, _ in matches)}")
    directory, info = matches[0]
    if not info.get("state") or not info.get("provenance"):
        print(f"legacy query run {directory.name}: provenance or completion state is unknown", file=sys.stderr)
    elif info["state"] != "complete":
        print(f"query run {directory.name} is {info['state']}; use bench inspect {directory.name}", file=sys.stderr)
    return directory


def _selected_run(name: str, run: str, selection: str | None) -> Path:
    if selection is not None:
        if run not in ("latest", "all"):
            raise SystemExit("choose either --selection or --run for query results")
        from .results import load_query_selection
        experiment = Path(name).stem if Path(name).suffix == ".toml" else name
        return load_query_selection(experiment, selection)
    return run_dir(name, run)


def load_rows(rd: Path) -> list[dict]:
    """One row per (program, query) with every engine's numbers side by side (times in ms)."""
    if (rd / "run.json").is_file():
        manifest = json.loads((rd / "run.json").read_text())
        if manifest.get("schema_version") == 1:
            if manifest.get("state") != "complete":
                raise SystemExit(f"query reports require a complete run; {rd.name} is {manifest.get('state', 'unknown')}. "
                                 f"Use bench inspect {rd.name} to inspect partial results.")
            artifacts.verify_run(rd)
            for program in manifest.get("programs", []):
                _validate_program_payload(rd / program, manifest.get("engines", []),
                                          no_decomp_only=manifest.get("config", {}).get("no_decomp_only", False))
    rows = []
    for pdir in sorted(d for d in rd.iterdir() if d.is_dir() and (d / "index.json").is_file()):
        idx = {q["id"]: q for q in json.loads((pdir / "index.json").read_text())["queries"]}
        merged = {qid: {"program": pdir.name, "query": qid, "name": q["name"], "ruleset": q.get("ruleset"), "atoms": q["atoms"]}
                  for qid, q in idx.items()}
        for line in (pdir / "egglog.jsonl").read_text().splitlines() if (pdir / "egglog.jsonl").exists() else []:
            r = json.loads(line); m = merged.get(r["query"])
            if m is None:
                continue
            key = "egglog" if r.get("engine_label") == "egglog-cracked" else ("egglog_nd" if r.get("no_decomp") else "egglog")
            if "error" in r:
                m[f"{key}_status"] = "error"; continue
            m[f"{key}_status"] = "ok"; m[f"{key}_rows"] = r["size"]
            m[f"{key}_execute"] = r["query_s"] * 1e3
            m[f"{key}_optimize"] = r["other_s"] * 1e3     # parse, typecheck, rule compilation
        for eng in ("duckdb", "umbra"):
            f = pdir / f"{eng}.jsonl"
            if not f.exists():
                continue
            for line in f.read_text().splitlines():
                r = json.loads(line); m = merged.get(r["query"])
                if m is None:
                    continue
                m[f"{eng}_status"] = r["status"]
                if r["status"] != "ok":
                    continue
                m[f"{eng}_rows"] = r["rows"]
                if eng == "duckdb":
                    m["duckdb_optimize"], m["duckdb_execute"] = r["optimize_ms"], r["execute_ms"]
                    m["duckdb_operators"] = r["operator_ms"]
                else:
                    m["umbra_optimize"], m["umbra_execute"], m["umbra_adhoc"] = r["prepare_ms"], r["execute_ms"], r["adhoc_ms"]
        rows += merged.values()
    return rows


SYSTEMS = [("egglog", "egglog-cracked"), ("egglog_nd", "egglog no-decomp"), ("duckdb", "DuckDB"), ("umbra", "Umbra")]


def present(rows):
    return [(k, label) for k, label in SYSTEMS if any(f"{k}_status" in r for r in rows)]


# ------------------------------------------------------------------ check
def check(name: str, run: str = "latest", selection: str | None = None) -> int:
    rows = load_rows(_selected_run(name, run, selection))
    systems = present(rows)
    bad = 0
    for r in rows:
        counts = {label: r[f"{k}_rows"] for k, label in systems if r.get(f"{k}_status") == "ok"}
        if len(set(counts.values())) > 1:
            bad += 1
            print(f"MISMATCH {r['program']}/{r['query']} ({r['name'][:50]}): {counts}")
    print(f"{len(rows)} queries compared across {[l for _, l in systems]}, {bad} mismatches")
    return 1 if bad else 0


# ------------------------------------------------------------------ summary
def summary(name: str, run: str = "latest", selection: str | None = None):
    rd = _selected_run(name, run, selection)
    rows = load_rows(rd)
    systems = present(rows)
    print(f"run {rd.name}: times in ms, summed over the queries that every system ran successfully and that return rows")
    hdr = f"{'program':12s}{'queries':>8s}{'w/ rows':>8s}{'common':>7s}" + "".join(f"{l[:16]:>34s}" for _, l in systems)
    sub = f"{'':35s}" + "".join(f"{'optimize':>12s}{'execute':>11s}{'fail':>11s}" for _ in systems)
    print(hdr); print(sub)
    for prog in sorted({r["program"] for r in rows}):
        pr = [r for r in rows if r["program"] == prog]
        withrows = [r for r in pr if r.get("egglog_rows", 0) > 0]
        common = [r for r in withrows if all(r.get(f"{k}_status") == "ok" for k, _ in systems)]
        line = f"{prog:12s}{len(pr):8d}{len(withrows):8d}{len(common):7d}"
        for k, _ in systems:
            fail = sum(1 for r in pr if r.get(f"{k}_status") not in (None, "ok"))
            line += f"{sum(r[f'{k}_optimize'] for r in common):12.1f}{sum(r[f'{k}_execute'] for r in common):11.1f}{fail:11d}"
        print(line)


# ------------------------------------------------------------------ plot
def plot(name: str, run: str = "latest", out_dir: Path | None = None, selection: str | None = None) -> list[Path]:
    import csv
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from .plot import MUTED, PALETTE  # shared style (rcParams are set on import)

    name = Path(name).stem if Path(name).suffix == ".toml" else name
    rd = _selected_run(name, run, selection)
    out_dir = out_dir or FIGURES
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = load_rows(rd)
    systems = present(rows)
    color = {k: PALETTE[i] for i, (k, _) in enumerate(systems)}
    written = []

    # CSV with every query side by side
    cols = ["program", "query", "name", "ruleset", "atoms"] + [f"{k}_{f}" for k, _ in systems for f in ("status", "rows", "optimize", "execute")]
    p = out_dir / f"{name}.csv"
    with p.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    written.append(p)

    progs = sorted({r["program"] for r in rows})
    common = [r for r in rows if r.get("egglog_rows", 0) > 0 and all(r.get(f"{k}_status") == "ok" for k, _ in systems)]

    # (1) per program: summed optimize/compile and execute time per system, one panel each, log scale
    fig, axes = plt.subplots(1, 2, figsize=(max(3.0, 0.5 * len(progs) * len(systems) / 2 + 1.5) * 2, 2.6), sharey=True)
    x = np.arange(len(progs)); wbar = 0.8 / len(systems)
    for ax, part, title in [(axes[0], "optimize", "optimize / compile"), (axes[1], "execute", "execute")]:
        for i, (k, label) in enumerate(systems):
            ys = [sum(r[f"{k}_{part}"] for r in common if r["program"] == p) for p in progs]
            ax.bar(x - 0.4 + wbar * (i + 0.5), ys, width=wbar * 0.92, color=color[k], label=label)
        ax.set_xticks(x); ax.set_xticklabels(progs, rotation=30, ha="right")
        ax.set_yscale("log"); ax.set_title(title); ax.grid(True, axis="y")
    axes[0].set_ylabel(f"ms, summed over {len(common)} queries")
    axes[1].legend(fontsize=6, frameon=False)
    fig.tight_layout()
    p = out_dir / f"{name}-totals.pdf"; fig.savefig(p, bbox_inches="tight"); fig.savefig(p.with_suffix(".png"), bbox_inches="tight"); plt.close(fig)
    written.append(p)

    # (2) per query: each system's execution time against egglog's, log-log
    others = [(k, l) for k, l in systems if k != "egglog"]
    fig, axes = plt.subplots(1, len(others), figsize=(2.8 * len(others), 2.8), squeeze=False, sharex=True, sharey=True)
    vals = [r[f"{k}_execute"] for r in common for k, _ in systems if r[f"{k}_execute"] > 0]
    lo, hi = (min(vals) / 1.5, max(vals) * 1.5) if vals else (0.01, 10)
    for ax, (k, label) in zip(axes[0], others):
        xs = [r["egglog_execute"] for r in common]; ys = [r[f"{k}_execute"] for r in common]
        ax.scatter(xs, ys, s=6, color=color[k], alpha=0.6, linewidths=0)
        ax.plot([lo, hi], [lo, hi], color=MUTED, linewidth=0.8, linestyle="--")
        ax.set_xscale("log"); ax.set_yscale("log"); ax.set_xlim(lo, hi); ax.set_ylim(lo, hi)
        faster = sum(1 for a, b in zip(xs, ys) if a < b)
        ax.set_title(f"{label}\negglog-cracked faster on {faster}/{len(xs)}", fontsize=8)
        ax.set_xlabel("egglog-cracked execution (ms)"); ax.grid(True, axis="both")
    axes[0][0].set_ylabel("execution (ms)")
    fig.tight_layout()
    p = out_dir / f"{name}-scatter.pdf"; fig.savefig(p, bbox_inches="tight"); fig.savefig(p.with_suffix(".png"), bbox_inches="tight"); plt.close(fig)
    written.append(p)
    return written
