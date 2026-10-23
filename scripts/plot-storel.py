#!/usr/bin/env python3
"""The STOREL figure: time to the node limit next to the e-graph size actually reached.

Usage: uv run python scripts/plot-storel.py --run RUN_ID
       uv run python scripts/plot-storel.py --selection NAME

One row per head-to-head comparison (egg vs egglog under the same scheduling discipline),
two panels per row: the wall time (median of the repetitions, min/max whiskers, log scale)
and the number of Term e-nodes at the stop, with the kernel's node limit as a dashed mark.
A run stops after the iteration that crosses the limit, so the size shows how far past it
each engine went (egglog's iterations are few and large, egg's many and small).
"""
from __future__ import annotations

import argparse
import csv
import json
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

from harness.config import experiment_from_dict
from harness.results import load_rows, read_selection, report_data

COMPARISONS = [  # (title, [(variant label, legend label, color)])
    ("Every rule every iteration (egg SimpleScheduler)",
     [("egg-simple", "egg", "#E6A117"), ("egglog-rust", "egglog", "#2864B4")]),
    ("Back-off scheduler (STOREL's configuration)",
     [("egg-backoff", "egg", "#E6A117"), ("egglog-rust-backoff", "egglog", "#2864B4")]),
]
KERNELS = ["mmm-sum-2nd", "mmm-2nd", "batax-1st", "mttkrp-2nd", "ttm-2nd", "batax-2nd"]
LABELS = {"mmm-sum-2nd": "ΣMMM 2nd", "mmm-2nd": "MMM 2nd", "batax-1st": "BATAX 1st",
          "mttkrp-2nd": "MTTKRP 2nd", "ttm-2nd": "TTM 2nd", "batax-2nd": "BATAX 2nd"}


def report_input(args):
    name = "storel"
    if args.selection:
        selected, _ = read_selection(name, args.selection)
        exp = experiment_from_dict(name, {**selected["config"], "metric": selected["metric"]})
        return exp, load_rows(exp, selection=args.selection)
    return report_data(name, args.run or "latest")


def node_limit(benchmark: str) -> int | None:
    """The limit from the benchmark program's marker line."""
    text = (ROOT / "benchmarks" / "storel" / f"{benchmark}.egg").read_text()
    m = re.search(r"^;; BENCHMARK_NODE_LIMIT (\d+)", text, re.M)
    return int(m.group(1)) if m else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--run", help="Latest complete run (default), exact ID or unique prefix/tag.")
    source.add_argument("--selection", help="Explicit result selection, using its frozen configuration.")
    parser.add_argument("--out", type=Path, default=ROOT / "figures" / "storel")
    args = parser.parse_args()
    exp, rows = report_input(args)
    assert all(r["status"] == "ok" for r in rows), "every STOREL measurement must have status ok"
    groups = defaultdict(list)
    for row in rows:
        groups[row["benchmark"], row["variant"]].append(row)
    kernels = [k for k in KERNELS if any(b == k for b, _ in groups)]

    plt.rcParams.update({"font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
                         "legend.fontsize": 8, "pdf.fonttype": 42, "savefig.facecolor": "white"})
    fig, axes = plt.subplots(len(COMPARISONS), 2, figsize=(10, 2.6 * len(COMPARISONS)), squeeze=False)
    x = np.arange(len(kernels))
    width = 0.36
    summary = []
    for r, (title, variants) in enumerate(COMPARISONS):
        for c, (metric, ylabel) in enumerate([("wall_s", "time to the node limit (s)"), ("nodes", "e-nodes at the stop")]):
            ax = axes[r][c]
            for i, (label, legend, color) in enumerate(variants):
                medians, lo, hi = [], [], []
                for k in kernels:
                    rs = groups.get((k, label), [])
                    vals = [row["wall_s"] if metric == "wall_s" else row["report"]["nodes"] for row in rs]
                    if not vals:
                        medians.append(np.nan); lo.append(0); hi.append(0)
                        continue
                    med = statistics.median(vals)
                    medians.append(med); lo.append(med - min(vals)); hi.append(max(vals) - med)
                    if metric == "wall_s":
                        summary.append({"comparison": title, "benchmark": k, "variant": label, "reps": len(vals),
                                        "wall_s_median": med, "wall_s_min": min(vals), "wall_s_max": max(vals),
                                        "nodes_median": statistics.median(row["report"]["nodes"] for row in rs),
                                        "iterations_median": statistics.median(row["report"]["iterations"] for row in rs)})
                ax.bar(x + (i - 0.5) * width, medians, width, yerr=[lo, hi], color=color, label=legend,
                       error_kw={"elinewidth": 0.8, "capsize": 2})
                if metric == "wall_s":
                    for xi, med in zip(x + (i - 0.5) * width, medians):
                        if not np.isnan(med):
                            ax.text(xi, med, f"{med:.2g}" if med < 10 else f"{med:.0f}", ha="center", va="bottom", fontsize=7)
            if metric == "nodes":
                for xi, k in zip(x, kernels):
                    limit = node_limit(k)
                    if limit:
                        ax.hlines(limit, xi - 0.45, xi + 0.45, colors="black", linestyles="dashed", linewidth=0.9,
                                  label="node limit" if xi == 0 else None)
            ax.set_yscale("log")
            ax.set_xticks(x)
            ax.set_xticklabels([LABELS.get(k, k) for k in kernels], rotation=20, ha="right")
            ax.set_ylabel(ylabel)
            ax.set_title(title if c == 0 else "e-graph size reached (limit dashed)")
            ax.grid(axis="y", linewidth=0.4, alpha=0.5)
            if r == 0 or c == 1:
                ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.out / "storel-comparison"
    for ext in (".pdf", ".png"):
        fig.savefig(stem.with_suffix(ext), dpi=180)
    with open(stem.with_suffix(".csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0]))
        w.writeheader()
        w.writerows(summary)
    (stem.with_suffix(".json")).write_text(json.dumps({"experiment": exp.name, "selection": args.selection,
                                                      "run": None if args.selection else args.run or "latest"}, indent=1))
    print(f"wrote {stem}.pdf/.png/.csv")


if __name__ == "__main__":
    main()
