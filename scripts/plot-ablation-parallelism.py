#!/usr/bin/env python3
"""Plot a completed query-parallelism ablation run, with size-validation context.

Usage: uv run python scripts/plot-ablation-parallelism.py --run RUN_ID
       uv run python scripts/plot-ablation-parallelism.py --selection NAME
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, MaxNLocator

from harness.config import experiment_from_dict, resolve_benchmarks
from harness.results import load_rows, read_selection, report_data

NAMES = {
    "egglog": "Vanilla",
    "inter-query-only": "Inter-query only",
    "inter-query-top-level": "Inter-query + top-level",
}
COLORS = ["#2864B4", "#D66027", "#19846A"]
MARKERS = ["o", "s", "^"]
SUITE_NAMES = {
    "math": "Math",
    "math-microbenchmark": "Math microbenchmarks",
    "horner": "Horner",
    "matmul-assoc": "Matrix-chain associativity",
    "actions": "Actions",
    "eggcc": "eggcc",
    "hardboiled": "Hardboiled",
    "pointer-analysis": "Pointer analysis",
    "luminal": "Luminal",
}


def report_input(args):
    """Use the same validated sources and frozen recipes as bench plot."""
    name = "ablation-parallelism"
    if args.selection:
        selected, _ = read_selection(name, args.selection)
        exp = experiment_from_dict(name, {**selected["config"], "metric": selected["metric"]})
        rows = load_rows(exp, selection=args.selection)
    else:
        exp, rows = report_data(name, args.run or "latest")
    return exp, rows


def configured_benchmarks(exp):
    # Captured recipes list exact IDs, allowing reporting without source datasets.
    # Legacy recipes may still require expansion of suite names or globs.
    if all("/" in spec and not any(c in spec for c in "*?[") for spec in exp.benchmarks):
        return {tuple(spec.split("/", 1)) for spec in exp.benchmarks}
    return {(b.suite, b.name) for b in resolve_benchmarks(exp.benchmarks)}


def timing_scope(row):
    aliases = {"process": "process", "analysis": "analysis", "egglog-analysis": "analysis"}
    values = [value for value in (row.get("timing_scope"), (row.get("report") or {}).get("timing_scope"))
              if value is not None]
    scopes = {aliases[value] for value in values} if values else {"process"}
    assert len(scopes) == 1, f"Conflicting row timing_scope metadata: {values!r}"
    return scopes.pop()


def geomean(values):
    values = list(values)
    assert values and all(v > 0 for v in values)
    return math.exp(statistics.mean(math.log(v) for v in values))


def csv_write(path, rows):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--run", help="Latest complete run (default), exact ID, unique prefix/tag, or all compatible disjoint runs.")
    source.add_argument("--selection", help="Explicit result selection, using its frozen configuration.")
    parser.add_argument("--out", type=Path, default=ROOT / "figures")
    args = parser.parse_args()
    exp, rows = report_input(args)
    variants = [v.label for v in exp.variants]
    threads = exp.threads
    benches = sorted(configured_benchmarks(exp))
    assert all(v.label in NAMES for v in exp.variants), "Unsupported ablation variant."
    assert all(v.threads is None or list(v.threads) == threads for v in exp.variants), "Ablation figures require the same thread counts for every variant."
    assert exp.baseline in variants, "The selection must include the configured baseline."
    assert len(rows) == len(benches) * len(variants) * len(threads) * exp.reps
    assert all(r["status"] == "ok" for r in rows)
    groups = defaultdict(list)
    for r in rows:
        groups[r["suite"], r["benchmark"], r["variant"], r["threads"]].append(r)
    expected_keys = {(s, b, v, t) for s, b in benches for v in variants for t in threads}
    assert set(groups) == expected_keys, "Missing or unexpected benchmark/variant/thread configurations."
    benchmark_scopes = {}
    for suite, bench in benches:
        scopes = {timing_scope(r) for r in rows if (r["suite"], r["benchmark"]) == (suite, bench)}
        assert len(scopes) == 1, f"Mixed timing scopes for {suite}/{bench}: {sorted(scopes)}"
        benchmark_scopes[suite, bench] = scopes.pop()
        for variant in variants:
            for thread in threads:
                group = groups[suite, bench, variant, thread]
                assert len(group) == exp.reps
                assert {r["rep"] for r in group} == set(range(exp.reps))
                assert all(r["report"].get("sizes") for r in group)
    medians = {k: statistics.median(r["wall_s"] for r in rs) for k, rs in groups.items()}
    size_fingerprints = defaultdict(set)
    same_thread_fingerprints = defaultdict(set)
    for r in rows:
        size_fingerprints[r["suite"], r["benchmark"]].add(json.dumps(r["report"]["sizes"], sort_keys=True))
        same_thread_fingerprints[r["suite"], r["benchmark"], r["threads"]].add(json.dumps(r["report"]["sizes"], sort_keys=True))
    mismatches = sorted(k for k, fingerprints in size_fingerprints.items() if len(fingerprints) > 1)
    same_thread_mismatches = sorted({(s, b) for (s, b, t), fingerprints in same_thread_fingerprints.items() if len(fingerprints) > 1})
    # Retain the same conservative cohort as the earlier run summary: exclude
    # the complete eggcc suite, including workloads with stable sizes.
    overview = [(s, b) for s, b in benches if s != "eggcc"]
    assert not any(k in mismatches for k in overview), "Overview cohort has a new size mismatch."
    assert overview, "The aggregate figure requires at least one benchmark outside eggcc."
    # Preserve the previous suite ordering even for sorted captured benchmark IDs.
    suites = [suite for suite in SUITE_NAMES if any(s == suite for s, _ in benches)]
    assert {s for s, _ in benches} == set(suites), "Unsupported benchmark suite."
    selected_scopes = set(benchmark_scopes.values())
    time_description = ("end-to-end process time" if selected_scopes == {"process"} else
                        "analysis time (loading excluded)" if selected_scopes == {"analysis"} else
                        "analysis time for loading-excluded suites; process time otherwise")
    run_ids = sorted({r["run_id"] for r in rows})
    source_label = f"Selection {args.selection}" if args.selection else f"Run {run_ids[0]}" if len(run_ids) == 1 else f"{len(run_ids)} disjoint runs"
    host = rows[0].get("host", {})
    cpu_label = host.get("cpu", "CPU unspecified")
    affinity_label = f"CPU affinity {exp.cpus}" if exp.cpus else "Unpinned CPU affinity"
    scopes = {"overview_excluding_eggcc": overview}
    scopes.update({s: [(suite, b) for suite, b in benches if suite == s] for s in suites})

    aggregate = []
    for scope, members in scopes.items():
        for variant in variants:
            for thread in threads:
                aggregate.append({
                    "scope": scope, "benchmarks": len(members), "variant": variant,
                    "threads": thread, "metric": "wall_s",
                    "timing_scopes": ",".join(sorted({benchmark_scopes[s, b] for s, b in members})),
                    "run_ids": ",".join(sorted({r["run_id"] for s, b in members for r in groups[s, b, variant, thread]})),
                    "total_wall_s": sum(medians[s, b, variant, thread] for s, b in members),
                    "scaling_geomean": geomean(medians[s, b, variant, threads[0]] / medians[s, b, variant, thread] for s, b in members),
                    "vs_vanilla_geomean": geomean(medians[s, b, exp.baseline, thread] / medians[s, b, variant, thread] for s, b in members),
                    "size_mismatch_benchmarks": sum((s, b) in mismatches for s, b in members),
                })
    data = {(r["scope"], r["variant"], r["threads"]): r for r in aggregate}
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.out / "ablation-parallelism-time-speedup"
    csv_write(stem.with_suffix(".csv"), aggregate)
    per_benchmark = []
    for (s, b, v, t), rs in sorted(groups.items()):
        values = [r["wall_s"] for r in rs]
        per_benchmark.append({
            "suite": s, "benchmark": b, "variant": v, "threads": t, "samples": len(values),
            "run_id": rs[0]["run_id"], "timing_scope": benchmark_scopes[s, b],
            "commit": rs[0].get("commit"),
            "machine_id": rs[0].get("machine_id", rs[0].get("host", {}).get("hostname")),
            "environment_id": rs[0].get("environment_id"),
            "wall_s_median": medians[s, b, v, t], "wall_s_min": min(values), "wall_s_max": max(values),
            "scaling": medians[s, b, v, threads[0]] / medians[s, b, v, t],
            "vs_vanilla": medians[s, b, exp.baseline, t] / medians[s, b, v, t],
            "size_mismatch": (s, b) in mismatches,
        })
    csv_write(stem.with_name(stem.name + "-benchmarks.csv"), per_benchmark)
    metadata = {
        "selection": args.selection, "requested_run": None if args.selection else args.run or "latest",
        "run_ids": run_ids, "run_id": run_ids[0] if len(run_ids) == 1 else None,
        "commits": sorted({r["commit"] for r in rows}),
        "egglog_commit": rows[0]["commit"] if len({r["commit"] for r in rows}) == 1 else None,
        "machine_id": rows[0].get("machine_id", host.get("hostname")),
        "environment_id": rows[0].get("environment_id"), "host": host,
        "provenance": "captured" if rows[0].get("environment_id") else "legacy-incomplete",
        "cpu": cpu_label, "cpus": exp.cpus,
        "metric": "wall_s", "timing_scopes": sorted(selected_scopes),
        "samples_per_configuration": exp.reps,
        "overview_benchmarks": len(overview), "overview_excluded_suite": "eggcc",
        "size_mismatch_benchmarks": [f"{s}/{b}" for s, b in mismatches],
        "same_thread_size_mismatch_benchmarks": [f"{s}/{b}" for s, b in same_thread_mismatches],
        "time_definition": f"Sum of per-benchmark median wall times ({time_description}); one complete pass, not the sum of all repetitions.",
        "scaling_definition": "Geometric mean of each benchmark's own variant T(1)/T(p).",
        "same_thread_definition": "Geometric mean of each benchmark's vanilla T(p)/variant T(p).",
        "limitations": [
            f"{exp.reps} repetitions per configuration; no confidence intervals claimed.",
            "Final-size differences also occur between repeated vanilla runs within eggcc.",
            "Observed single-thread Gemma timing drift; small differences are preliminary.",
            "No confidence intervals claimed. Equal relation sizes do not prove semantic equivalence.",
        ],
    }
    stem.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10, "axes.titlesize": 12,
        "axes.labelsize": 10, "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#B9C1CA", "axes.labelcolor": "#263243", "text.color": "#263243",
        "xtick.color": "#4D5967", "ytick.color": "#4D5967", "grid.color": "#E4E8ED",
        "grid.linewidth": 0.7, "axes.axisbelow": True, "pdf.fonttype": 42,
        "svg.fonttype": "none", "savefig.facecolor": "white",
    })
    handles = [Line2D([0], [0], color=c, marker=m, lw=2.2, ms=6, label=NAMES[v])
               for v, c, m in zip(variants, COLORS, MARKERS)]

    def panel(ax, scope, field, annotate=False):
        for v, color, marker in zip(variants, COLORS, MARKERS):
            values = [data[scope, v, t][field] for t in threads]
            ax.plot(threads, values, color=color, marker=marker, lw=2.2, ms=6, zorder=3)
            if annotate:
                label = f"{values[-1]:.1f}s" if field == "total_wall_s" else f"{values[-1]:.2f}×"
                offset = {"egglog": 0, "inter-query-only": 17, "inter-query-top-level": -17}[v] if field == "total_wall_s" else 0
                ax.annotate(label, (threads[-1], values[-1]), xytext=(12, offset), textcoords="offset points",
                            color=color, fontsize=10, va="center", fontweight="bold",
                            arrowprops={"arrowstyle": "-", "color": color, "lw": 0.7} if offset else None)
        ax.set_xscale("log", base=2)
        ax.set_xticks(threads, [str(t) for t in threads])
        ax.set_xlim(threads[0] * 0.9, threads[-1] * (1.5 if annotate else 1.1))
        ax.set_xlabel("Worker threads")
        ax.grid(axis="y")
        if field == "total_wall_s":
            ax.set_ylim(bottom=0)
            ax.set_ylabel("Sum of median wall times (s)")
            ax.yaxis.set_major_locator(MaxNLocator(5))
        else:
            ax.axhline(1, color="#8994A1", ls=(0, (3, 3)), lw=1, zorder=1)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:.2f}×"))
            ax.yaxis.set_major_locator(MaxNLocator(5))
            ax.set_ylabel("Geometric mean speedup")
            ax.margins(y=0.18)

    fig, axes = plt.subplots(1, 3, figsize=(14.5, 5.5))
    fig.subplots_adjust(left=0.065, right=0.98, bottom=0.24, top=0.70, wspace=0.32)
    fig.suptitle("Query parallelism: time and speedup", x=0.065, y=0.98, ha="left", fontsize=19, fontweight="bold")
    fig.text(0.065, 0.90, f"{len(overview)} benchmarks · eggcc excluded · median of {exp.reps} runs per configuration · {time_description}", fontsize=11)
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.055, 0.86), ncol=3, frameon=False, columnspacing=2.5)
    fields = ["total_wall_s", "scaling_geomean", "vs_vanilla_geomean"]
    titles = ["A  Total wall time", "B  Scaling from one thread", "C  Gain over vanilla"]
    for ax, field, title in zip(axes, fields, titles):
        panel(ax, "overview_excluding_eggcc", field, annotate=True)
        ax.set_title(title, loc="left", pad=15, fontweight="bold")
    fig.text(0.065, 0.145, f"Time: sum of medians for one pass through the {len(overview)} benchmarks. Speedups: each benchmark has equal weight.", fontsize=9)
    fig.text(0.065, 0.103, "B: each variant’s T(1) / T(p).  C: vanilla T(p) / variant T(p); values above 1× favor the restricted mode.", fontsize=9)
    fig.text(0.065, 0.061, f"Preliminary: sequential run order and timing drift. eggcc: {len(mismatches)} programs differ in sizes across configurations ({len(same_thread_mismatches)} at fixed thread count).", fontsize=9, color="#6B5360")
    fig.text(0.065, 0.022, f"{source_label}  ·  {affinity_label}  ·  {cpu_label}", fontsize=8, color="#74808C")
    for suffix in (".pdf", ".png", ".svg"):
        fig.savefig(stem.with_suffix(suffix), dpi=200)
    plt.close(fig)

    # All suites, including the flagged eggcc suite, in a readable three-page PDF.
    detail_path = args.out / "ablation-parallelism-by-suite.pdf"
    with PdfPages(detail_path) as pdf:
        for page in range(math.ceil(len(suites) / 3)):
            page_suites = suites[page * 3:(page + 1) * 3]
            fig, axes = plt.subplots(3, 2, figsize=(12, 12.2))
            fig.subplots_adjust(left=0.10, right=0.97, bottom=0.12, top=0.85, hspace=0.70, wspace=0.32)
            fig.suptitle(f"Query parallelism by suite  ·  {page + 1}/{math.ceil(len(suites) / 3)}", x=0.10, y=0.98, ha="left", fontsize=18, fontweight="bold")
            fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.09, 0.945), ncol=3, frameon=False)
            fig.text(0.10, 0.895, "Left: total wall time for one pass. Right: geometric mean speedup over vanilla at the same thread count.", fontsize=10)
            for idx, suite in enumerate(page_suites):
                n = len(scopes[suite])
                flagged = sum(k in mismatches for k in scopes[suite])
                for col, field in enumerate(("total_wall_s", "vs_vanilla_geomean")):
                    ax = axes[idx, col]
                    panel(ax, suite, field)
                    title = f"{SUITE_NAMES[suite]} · {n} benchmark{'s' if n != 1 else ''}"
                    if flagged:
                        ax.set_facecolor("#FFF3F2")
                        title += f"\n{flagged} programs differ in sizes across configurations"
                    ax.set_title(title, loc="left", fontsize=11, fontweight="bold", color="#A44A44" if flagged else "#263243", pad=12)
            for idx in range(len(page_suites), 3):
                for ax in axes[idx]:
                    ax.set_visible(False)
            fig.text(0.10, 0.065, f"Medians of {exp.reps} repetitions. No confidence intervals claimed. Observed timing drift limits small differences.", fontsize=8.5)
            fig.text(0.10, 0.040, "eggcc timings are shown for completeness; differing relation sizes prevent treating them as controlled equal-work comparisons.", fontsize=8.5, color="#8F4A49")
            fig.text(0.10, 0.020, source_label, fontsize=8, color="#74808C")
            pdf.savefig(fig)
            fig.savefig(args.out / f"ablation-parallelism-by-suite-{page + 1}.png", dpi=150)
            plt.close(fig)
    print(f"Validated {len(rows)} samples; {len(mismatches)} programs have final-size variation.")
    for v in variants:
        record = data["overview_excluding_eggcc", v, threads[-1]]
        print(NAMES[v], {key: round(record[key], 4) for key in fields})
    print(stem.with_suffix(".png"))
    print(stem.with_suffix(".pdf"))
    print(detail_path)


if __name__ == "__main__":
    main()
