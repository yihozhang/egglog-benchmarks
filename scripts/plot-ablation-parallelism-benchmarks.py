#!/usr/bin/env python3
"""One wall-time scaling figure per suite, with one panel per benchmark.

Usage: uv run python scripts/plot-ablation-parallelism-benchmarks.py --run RUN_ID
       uv run python scripts/plot-ablation-parallelism-benchmarks.py --selection NAME
       uv run python scripts/plot-ablation-parallelism-benchmarks.py --run RUN_ID --suite pointer-analysis

Missing timing_scope metadata means process wall time. Analysis measurements
exclude loading and setup and use separate filenames to preserve historical figures.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, MaxNLocator

from harness.config import experiment_from_dict, resolve_benchmarks
from harness.results import load_rows, read_selection, report_data

VARIANT_STYLE = {
    "egglog": ("Vanilla", "#2864B4", "o", "-"),
    "inter-query-only": ("Inter-query only", "#D66027", "s", "--"),
    "inter-query-top-level": ("Inter-query + top-level", "#19846A", "^", "-."),
}
GROUPS = [
    ("math-horner-matrix", "Math microbenchmarks + Horner + matrix-chain",
     ["math-microbenchmark", "horner", "matmul-assoc"], 4),
    ("eggcc", "eggcc", ["eggcc"], 4),
    ("luminal", "Luminal", ["luminal"], 4),
    ("math", "Math", ["math"], 1),
    ("actions", "Actions", ["actions"], 1),
    ("hardboiled", "Hardboiled", ["hardboiled"], 6),
    ("pointer-analysis", "Pointer analysis", ["pointer-analysis"], 5),
]


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
    """Read adapter timing metadata without relabeling historical measurements."""
    aliases = {"process": "process", "analysis": "analysis", "egglog-analysis": "analysis"}
    report = row.get("report") or {}
    values = [value for value in (row.get("timing_scope"), report.get("timing_scope"))
              if value is not None]
    if not values:
        return "process"
    try:
        scopes = {aliases[value] for value in values}
    except (KeyError, TypeError) as error:
        raise ValueError(f"Unsupported timing_scope metadata: {values!r}") from error
    if len(scopes) != 1:
        raise ValueError(f"Conflicting row timing_scope metadata: {values!r}")
    return scopes.pop()


def natural_key(name):
    return tuple((1, int(part)) if part.isdigit() else (0, part)
                 for part in re.split(r"(\d+)", name))


def title_for(suite, benchmark):
    if suite == "matmul-assoc":
        return benchmark.replace("matmul-assoc-", "matrix-chain-")
    return benchmark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--run", help="Latest complete run (default), exact ID, unique prefix/tag, or all compatible disjoint runs.")
    source.add_argument("--selection", help="Explicit result selection, using its frozen configuration.")
    parser.add_argument("--out", type=Path, default=ROOT / "figures")
    parser.add_argument("--suite", action="append",
                        choices=sorted({suite for _, _, suites, _ in GROUPS for suite in suites}),
                        help="Plot only this suite; may be repeated. Leaves the full-run manifest untouched.")
    args = parser.parse_args()
    exp, rows = report_input(args)
    selected_suites = set(args.suite) if args.suite else None
    expected_benchmarks = {(suite, name) for suite, name in configured_benchmarks(exp)
                           if selected_suites is None or suite in selected_suites}
    if selected_suites is not None:
        rows = [row for row in rows if row["suite"] in selected_suites]
    assert rows, "No measurements found for the selected suites."
    assert expected_benchmarks, "No configured benchmarks match the selected suites."
    assert all(r["status"] == "ok" for r in rows)
    variants = [v.label for v in exp.variants]
    threads = exp.threads
    assert all(v.label in VARIANT_STYLE for v in exp.variants), "Unsupported ablation variant."
    assert all(v.threads is None or list(v.threads) == threads for v in exp.variants), "Ablation figures require the same thread counts for every variant."
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["suite"], row["benchmark"], row["variant"], row["threads"]].append(row)
    expected_keys = {(s, b, v, t) for s, b in expected_benchmarks for v in variants for t in threads}
    assert set(grouped) == expected_keys, "Missing or unexpected benchmark/variant/thread configurations."
    assert len(rows) == len(expected_keys) * exp.reps, "Incomplete repetition groups for the selected recipe."
    summary = {}
    for suite, bench in expected_benchmarks:
        for variant in variants:
            for thread in threads:
                key = suite, bench, variant, thread
                samples = grouped[key]
                assert len(samples) == exp.reps
                assert {r["rep"] for r in samples} == set(range(exp.reps))
                scopes = {timing_scope(row) for row in samples}
                assert len(scopes) == 1, f"Mixed timing scopes for {key}: {sorted(scopes)}"
                values = [r["wall_s"] for r in samples]
                assert all(v > 0 and math.isfinite(v) for v in values)
                summary[key] = {
                    "suite": suite, "benchmark": bench, "variant": variant,
                    "threads": thread, "samples": len(values),
                    "timing_scope": scopes.pop(), "run_id": samples[0]["run_id"],
                    "commit": samples[0].get("commit"),
                    "machine_id": samples[0].get("machine_id", samples[0].get("host", {}).get("hostname")),
                    "environment_id": samples[0].get("environment_id"),
                    "wall_s_median": statistics.median(values),
                    "wall_s_min": min(values), "wall_s_max": max(values),
                }

    # Validate every selected figure before writing any output.
    figure_scopes = {}
    for slug, _, suites, _ in GROUPS:
        scopes = {record["timing_scope"] for record in summary.values()
                  if record["suite"] in suites}
        assert len(scopes) <= 1, f"Mixed timing scopes in figure {slug}: {sorted(scopes)}"
        if scopes:
            figure_scopes[slug] = scopes.pop()

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9, "axes.titlesize": 10.5,
        "axes.labelsize": 10, "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#BCC5D0", "axes.labelcolor": "#263243", "text.color": "#263243",
        "xtick.color": "#4D5967", "ytick.color": "#4D5967", "grid.color": "#E5EAF0",
        "grid.linewidth": 0.65, "axes.axisbelow": True, "pdf.fonttype": 42,
        "svg.fonttype": "none", "savefig.facecolor": "white",
    })
    handles = [Line2D([0], [0], label=VARIANT_STYLE[v][0], color=VARIANT_STYLE[v][1],
                      marker=VARIANT_STYLE[v][2], ls=VARIANT_STYLE[v][3], lw=1.8,
                      ms=5, markerfacecolor="white") for v in variants]
    args.out.mkdir(parents=True, exist_ok=True)
    plotted = set()
    outputs = []
    for slug, group_title, suites, columns in GROUPS:
        members = [(s, b) for s in suites for b in sorted(
            (name for suite, name in expected_benchmarks if suite == s), key=natural_key)]
        if not members:
            continue
        assert not (set(members) & plotted), "A benchmark appears in multiple figures."
        plotted.update(members)
        scope = figure_scopes[slug]
        metric_label = "analysis wall time (loading excluded)" if scope == "analysis" else "wall time"
        nrows = math.ceil(len(members) / columns)
        width = 4.8 if columns == 1 else 3.35 * columns + 0.5
        height = 2.40 * nrows + (2.25 if slug == "eggcc" else 2.05)
        fig, axes = plt.subplots(nrows, columns, figsize=(width, height), squeeze=False)
        # Physical header/footer margins stay constant as the grid gets taller.
        fig.subplots_adjust(left=0.72 / width, right=1 - 0.25 / width,
                            top=1 - 1.30 / height, bottom=0.80 / height,
                            hspace=0.58, wspace=0.33)
        fig.text(0.72 / width, 1 - 0.23 / height, group_title,
                 fontsize=17 if columns > 1 else 15, fontweight="bold", va="top")
        fig.text(0.72 / width, 1 - 0.61 / height,
                 f"{len(members)} benchmark{'s' if len(members) != 1 else ''} · {metric_label} · median of {exp.reps} runs",
                 fontsize=10, va="top")
        fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.60 / width, 1 - 0.83 / height),
                   ncol=3 if columns > 1 else 1, frameon=False, fontsize=9 if columns > 1 else 8,
                   columnspacing=2.1, handlelength=2.8, borderaxespad=0)
        if columns == 1:
            # Single-benchmark figures need room for a vertical legend.
            fig.subplots_adjust(top=1 - 1.65 / height)
        for ax, (suite, bench) in zip(axes.flat, members):
            upper = 0.0
            for variant in variants:
                name, color, marker, linestyle = VARIANT_STYLE[variant]
                records = [summary[suite, bench, variant, t] for t in threads]
                y = np.array([r["wall_s_median"] for r in records])
                low = np.array([r["wall_s_min"] for r in records])
                high = np.array([r["wall_s_max"] for r in records])
                upper = max(upper, float(high.max()))
                line = ax.errorbar(threads, y, yerr=np.vstack((y - low, high - y)),
                                  color=color, marker=marker, ls=linestyle, lw=1.65, ms=4.4,
                                  markerfacecolor="white", markeredgewidth=1.1,
                                  elinewidth=0.9, capsize=2.0, capthick=0.8, zorder=3)
                for part in (*line[1], *line[2]):
                    part.set_alpha(0.55)
            ax.set_title(title_for(suite, bench), loc="left", pad=9, fontweight="bold")
            ax.set_xscale("log", base=2)
            ax.set_xticks(threads, [str(t) for t in threads])
            ax.set_xlim(threads[0] * 0.88, threads[-1] * (9.1 / 8))
            ax.set_ylim(0, upper * 1.13)
            ax.yaxis.set_major_locator(MaxNLocator(4, min_n_ticks=3))
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.3g}"))
            ax.tick_params(labelsize=8, length=3)
            ax.grid(axis="y")
        for ax in list(axes.flat)[len(members):]:
            ax.set_visible(False)
        fig.supylabel("Analysis wall time (s)" if scope == "analysis" else "Wall time (s)",
                      x=0.06 / width, fontsize=11)
        fig.supxlabel("Worker threads", y=0.43 / height, fontsize=11)
        note = f"Bars: min–max across {exp.reps} runs. Independent linear y-scales, starting at zero."
        if scope == "analysis":
            note += " Input loading and setup excluded."
        if slug == "eggcc":
            note += " All eggcc benchmarks included."
        fig.text(0.72 / width, 0.15 / height, note, fontsize=8 if columns > 1 else 6.2, color="#687788")
        metric_slug = "analysis-wall-time" if scope == "analysis" else "wall-time"
        stem = args.out / f"ablation-parallelism-{slug}-{metric_slug}"
        for ext in (".pdf", ".png", ".svg"):
            fig.savefig(stem.with_suffix(ext), dpi=180)
        plt.close(fig)
        points = [summary[s, b, v, t] for s, b in members for v in variants for t in threads]
        with stem.with_suffix(".csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(points[0]))
            writer.writeheader()
            writer.writerows(points)
        outputs.append({"group": slug, "title": group_title, "benchmarks": len(members),
                        "members": [f"{s}/{b}" for s, b in members], "stem": stem.name,
                        "timing_scope": scope, "sample_count": sum(point["samples"] for point in points)})
        print(f"{slug}: {len(members)} panels, {len(points)} measured points -> {stem.with_suffix('.pdf')}")
    assert plotted == expected_benchmarks, "Some benchmarks are missing from the figures."
    metadata = {
        "selection": args.selection, "requested_run": None if args.selection else args.run or "latest",
        "run_ids": sorted({r["run_id"] for r in rows}),
        "run_id": rows[0]["run_id"] if len({r["run_id"] for r in rows}) == 1 else None,
        "commits": sorted({r["commit"] for r in rows}),
        "egglog_commit": rows[0]["commit"] if len({r["commit"] for r in rows}) == 1 else None,
        "machine_id": rows[0].get("machine_id", rows[0].get("host", {}).get("hostname")),
        "environment_id": rows[0].get("environment_id"), "host": rows[0].get("host", {}),
        "cpu_affinity": exp.cpus,
        "provenance": "captured" if rows[0].get("environment_id") else "legacy-incomplete",
        "metric": "wall_s", "statistic": "median", "error_bars": "min–max, not confidence intervals",
        "repetitions": exp.reps, "threads": threads, "variants": variants,
        "benchmark_count": len(plotted), "sample_count": len(rows), "figures": outputs,
        "timing_scopes": sorted(set(figure_scopes.values())),
        "notes": [
            "Every benchmark is shown separately; no suite aggregation or speedup normalization.",
            "Sequential configuration order; observed timing drift is visible in the data.",
        ],
    }
    if "eggcc" in {suite for suite, _ in plotted}:
        metadata["notes"].append(
            "eggcc differences concern 12 extraction/analysis auxiliary tables; primary expression table sizes agree.")
    for output in outputs:
        member_ids = set(output["members"])
        source_run_ids = sorted({row["run_id"] for row in rows
                                 if f"{row['suite']}/{row['benchmark']}" in member_ids})
        suite_metadata = {**metadata, "figures": [output], "run_ids": source_run_ids,
                          "run_id": source_run_ids[0] if len(source_run_ids) == 1 else None,
                          "benchmark_count": output["benchmarks"],
                          "sample_count": output["sample_count"],
                          "timing_scope": output["timing_scope"],
                          "timing_scopes": [output["timing_scope"]]}
        manifest = args.out / f"{output['stem']}-manifest.json"
        manifest.write_text(json.dumps(suite_metadata, indent=2) + "\n")
    if selected_suites is None:
        (args.out / "ablation-parallelism-wall-time-manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Validated and plotted all {len(plotted)} benchmarks ({len(rows)} samples).")


if __name__ == "__main__":
    main()
