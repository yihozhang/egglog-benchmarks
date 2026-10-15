"""Query-only experiments (experiments/<name>.toml with kind = "query").

`bench run <name>` builds the query driver at `egglog_ref`, and for every program of `suite`
exports the final state, translates every rule, runs every rule in egglog (and with
no-decomp), then runs every query in each SQL engine. Working files (exports, generated
queries) go to build/query/<name>/<run-id>/; results to results/<name>/<run-id>/<program>/.

`bench summary|check|plot <name>` read the latest run (or --run <id>).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tomllib
from pathlib import Path

from .config import EXPERIMENTS, FIGURES, RESULTS, ROOT
from .runner import host_info, repo_commit

QDIR = ROOT / "systems" / "egglog-query"


def is_query(name: str) -> bool:
    p = EXPERIMENTS / f"{name}.toml"
    return p.exists() and tomllib.loads(p.read_text()).get("kind") == "query"


def load(name: str) -> dict:
    cfg = tomllib.loads((EXPERIMENTS / f"{name}.toml").read_text())
    cfg["name"] = name
    return cfg


def programs_of(cfg) -> list[str]:
    sdir = ROOT / "benchmarks" / cfg["suite"]
    return cfg.get("programs") or sorted(p.stem for p in sdir.glob("*.egg") if "header" not in p.stem)


# ------------------------------------------------------------------ run
def run(name: str, programs: list[str] | None = None, engines: list[str] | None = None, tag: str | None = None):
    cfg = load(name)
    programs = programs or programs_of(cfg)
    engines = engines or cfg.get("engines", [])
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{tag}" if tag else "")
    work = ROOT / "build" / "query" / name / run_id
    out = RESULTS / name / run_id
    out.mkdir(parents=True, exist_ok=True)
    driver = subprocess.run([str(QDIR / "build"), cfg["egglog_ref"]], capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    commit = subprocess.run(["git", "-C", str(ROOT / "egglog"), "rev-parse", f"{cfg['egglog_ref']}^{{commit}}"],
                            capture_output=True, text=True).stdout.strip()
    (out / "run.json").write_text(json.dumps({
        "experiment": name, "run_id": run_id, "config": {k: v for k, v in cfg.items() if k != "name"},
        "egglog_commit": commit, "driver": driver, "host": host_info(), "harness_commit": repo_commit(),
        "started": dt.datetime.now().isoformat(timespec="seconds"), "programs": programs, "engines": engines,
    }, indent=1))
    print(f"query experiment {name}: run {run_id}; {len(programs)} programs, engines egglog + {engines}")
    env = {**os.environ, "EGGLOG_QUERY_DRIVER": driver, "BENCH_SCRATCH": str(work / "scratch")}
    py = sys.executable
    for prog in programs:
        print(f"[{dt.datetime.now():%H:%M:%S}] {prog}: export, translate, egglog", flush=True)
        subprocess.run([py, str(QDIR / "run_suite.py"), cfg["suite"], str(work), prog], env=env, check=True)
        pdir = work / prog
        (out / prog).mkdir(exist_ok=True)
        for f in ("index.json", "egglog.jsonl"):
            shutil.copy(pdir / f, out / prog / f)
        for eng in engines:
            print(f"[{dt.datetime.now():%H:%M:%S}] {prog}: {eng}", flush=True)
            subprocess.run([py, str(QDIR / "bench_db.py"), eng, str(pdir), "--threads", str(cfg.get("threads", 1)),
                            "--reps", str(cfg.get("reps", 3)), "--timeout", str(cfg.get("timeout", 60))], env=env, check=True)
            shutil.copy(pdir / f"{eng}.jsonl", out / prog / f"{eng}.jsonl")
    print(f"results in {out.relative_to(ROOT)}")
    return out


# ------------------------------------------------------------------ load
def run_dir(name: str, run: str = "latest") -> Path:
    runs = sorted(d for d in (RESULTS / name).iterdir() if d.is_dir() and (d / "run.json").exists())
    if not runs:
        raise SystemExit(f"no results for query experiment {name}")
    if run in ("latest", "all"):
        return runs[-1]
    hit = [d for d in runs if d.name.startswith(run) or d.name.endswith("-" + run)]
    if not hit:
        raise SystemExit(f"no run of {name} matches {run!r}")
    return hit[-1]


def load_rows(rd: Path) -> list[dict]:
    """One row per (program, query) with every engine's numbers side by side (times in ms)."""
    rows = []
    for pdir in sorted(d for d in rd.iterdir() if d.is_dir()):
        idx = {q["id"]: q for q in json.load(open(pdir / "index.json"))["queries"]}
        merged = {qid: {"program": pdir.name, "query": qid, "name": q["name"], "ruleset": q.get("ruleset"), "atoms": q["atoms"]}
                  for qid, q in idx.items()}
        for line in open(pdir / "egglog.jsonl"):
            r = json.loads(line); m = merged.get(r["query"])
            if m is None:
                continue
            key = "egglog_nd" if r.get("no_decomp") else "egglog"
            if "error" in r:
                m[f"{key}_status"] = "error"; continue
            m[f"{key}_status"] = "ok"; m[f"{key}_rows"] = r["size"]
            m[f"{key}_execute"] = r["query_s"] * 1e3
            m[f"{key}_optimize"] = r["other_s"] * 1e3     # parse, typecheck, rule compilation
        for eng in ("duckdb", "umbra"):
            f = pdir / f"{eng}.jsonl"
            if not f.exists():
                continue
            for line in open(f):
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


SYSTEMS = [("egglog", "egglog"), ("egglog_nd", "egglog no-decomp"), ("duckdb", "DuckDB"), ("umbra", "Umbra")]


def present(rows):
    return [(k, label) for k, label in SYSTEMS if any(f"{k}_status" in r for r in rows)]


# ------------------------------------------------------------------ check
def check(name: str, run: str = "latest") -> int:
    rows = load_rows(run_dir(name, run))
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
def summary(name: str, run: str = "latest"):
    rd = run_dir(name, run)
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
def plot(name: str, run: str = "latest", out_dir: Path | None = None) -> list[Path]:
    import csv
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from .plot import MUTED, PALETTE  # shared style (rcParams are set on import)

    rd = run_dir(name, run)
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
        ax.set_title(f"{label}: egglog faster on {faster}/{len(xs)}")
        ax.set_xlabel("egglog execution (ms)"); ax.grid(True, axis="both")
    axes[0][0].set_ylabel("execution (ms)")
    fig.tight_layout()
    p = out_dir / f"{name}-scatter.pdf"; fig.savefig(p, bbox_inches="tight"); fig.savefig(p.with_suffix(".png"), bbox_inches="tight"); plt.close(fig)
    written.append(p)
    return written
