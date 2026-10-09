"""Figures. One function per plot kind; `plot_experiment` dispatches on experiment.plot."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from .config import Experiment  # noqa: E402
from .results import geomean, summarize  # noqa: E402

# Fixed categorical order (colorblind-validated); variants take colors in experiment order.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MUTED = "#9a9892"

plt.rcParams.update({
    "font.size": 8,
    "axes.titlesize": 9,
    "axes.labelsize": 8,
    "legend.fontsize": 7,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.grid.axis": "y",
    "grid.color": "#e4e3df",
    "grid.linewidth": 0.5,
    "axes.axisbelow": True,
    "pdf.fonttype": 42,
    "figure.dpi": 150,
})


def plot_experiment(exp: Experiment, rows: list[dict], out_dir: Path, metric: str = "wall_s") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summ = summarize(rows, metric)
    suffix = "" if metric == "wall_s" else "-" + metric.split(".")[-1]
    written = [write_csv(exp, summ, out_dir / f"{exp.name}{suffix}.csv")]
    if exp.plot == "scaling":
        written += plot_scaling(exp, summ, out_dir / f"{exp.name}{suffix}")
    else:
        written += plot_bars(exp, summ, out_dir / f"{exp.name}{suffix}")
    return written


def write_csv(exp: Experiment, summ: dict, path: Path) -> Path:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["suite", "benchmark", "variant", "threads", "status", "n", "median", "min", "max"])
        for (suite, name, variant, t), s in sorted(summ.items()):
            w.writerow([suite, name, variant, t, s["status"], s["n"], s["median"], s["min"], s["max"]])
    return path


def _suites(summ):
    suites: dict[str, list[str]] = {}
    for suite, name, _, _ in summ:
        if name not in suites.setdefault(suite, []):
            suites[suite].append(name)
    return {s: sorted(ns) for s, ns in suites.items()}


def _grid(suites: dict[str, list[str]], height=2.2):
    widths = [max(1.2, 0.18 * len(ns) + 0.6) for ns in suites.values()]
    fig, axes = plt.subplots(
        1, len(suites), figsize=(min(sum(widths), 14), height),
        gridspec_kw={"width_ratios": widths}, squeeze=False,
    )
    return fig, axes[0]


def _log_axis(ax, axis="y", base=10):
    """Log scale with plain-number ticks (0.1, 1, 10) and 2/5 minor labels when the range is narrow."""
    from matplotlib import ticker

    a = ax.yaxis if axis == "y" else ax.xaxis
    (ax.set_yscale if axis == "y" else ax.set_xscale)("log", base=base)
    fmt = ticker.FuncFormatter(lambda v, _: f"{v:g}")
    a.set_major_locator(ticker.LogLocator(base=base))
    a.set_major_formatter(fmt)
    lo, hi = (ax.get_ylim() if axis == "y" else ax.get_xlim())
    if hi / lo < 100 and base == 10:
        a.set_minor_locator(ticker.LogLocator(base=base, subs=(2.0, 5.0)))
        a.set_minor_formatter(fmt)
    else:
        a.set_minor_formatter(ticker.NullFormatter())


def _floor(ys) -> float:
    """A power-of-ten floor below the smallest value, so log-scale bars have a common base."""
    ys = [y for y in ys if y is not None and not np.isnan(y) and y > 0]
    if not ys:
        return 0.1
    return 10 ** np.floor(np.log10(min(ys) / 1.5))


def _xlabels(ax, names):
    ax.set_xticks(np.arange(len(names)))
    ax.set_xticklabels(names, rotation=90 if len(names) > 1 else 0)
    if len(names) > 12:
        ax.tick_params(axis="x", labelsize=5)


def plot_bars(exp: Experiment, summ: dict, stem: Path) -> list[Path]:
    """Grouped bars per benchmark (log time), one subplot per suite; plus a speedup figure if there is a baseline."""
    suites = _suites(summ)
    variants = [v.label for v in exp.variants]
    threads = sorted({k[3] for k in summ})
    written = []
    for t in threads:
        tsuf = f"-t{t}" if len(threads) > 1 else ""
        # --- absolute times
        fig, axes = _grid(suites)
        for ax, (suite, names) in zip(axes, suites.items()):
            x = np.arange(len(names))
            w = 0.8 / len(variants)
            series = []
            for v in variants:
                ys, fails = [], []
                for n in names:
                    s_ = summ.get((suite, n, v, t))
                    ys.append(s_["median"] if s_ and s_["status"] == "ok" else np.nan)
                    fails.append(s_["status"] if s_ and s_["status"] not in ("ok", "unsupported") else None)
                series.append((ys, fails))
            floor = _floor([y for ys, _ in series for y in ys])
            for i, (v, (ys, fails)) in enumerate(zip(variants, series)):
                xs = x - 0.4 + w * (i + 0.5)
                ax.bar(xs, np.array(ys) - floor, bottom=floor, width=w * 0.92,
                       color=PALETTE[i % len(PALETTE)], label=v if ax is axes[0] else None)
                for xi, f in zip(xs, fails):
                    if f:  # timeout / fail: a marker at the floor
                        ax.text(xi, floor, "×", ha="center", va="bottom", fontsize=7, color=PALETTE[i % len(PALETTE)])
            ax.set_ylim(bottom=floor)
            _log_axis(ax)
            _xlabels(ax, names)
            ax.set_title(suite)
        axes[0].set_ylabel("time (s)")
        fig.legend(loc="upper center", ncol=len(variants), bbox_to_anchor=(0.5, 1.08), frameon=False)
        fig.tight_layout()
        p = stem.with_name(stem.name + f"{tsuf}-time.pdf")
        fig.savefig(p, bbox_inches="tight"); fig.savefig(p.with_suffix(".png"), bbox_inches="tight")
        plt.close(fig)
        written.append(p)

        # --- speedup relative to baseline
        if exp.baseline:
            others = [v for v in variants if v != exp.baseline]
            fig, axes = _grid(suites)
            for ax, (suite, names) in zip(axes, suites.items()):
                x = np.arange(len(names))
                w = 0.8 / max(1, len(others))
                titles = []
                all_ys = []
                for i, v in enumerate(others):
                    ci = variants.index(v)
                    ys = []
                    for n in names:
                        sb = summ.get((suite, n, exp.baseline, t))
                        sv = summ.get((suite, n, v, t))
                        ok = sb and sv and sb["status"] == "ok" and sv["status"] == "ok"
                        ys.append(sb["median"] / sv["median"] if ok else np.nan)
                    xs = x - 0.4 + w * (i + 0.5)
                    ax.bar(xs, np.array(ys) - 1, bottom=1, width=w * 0.92,
                           color=PALETTE[ci % len(PALETTE)], label=v if ax is axes[0] else None)
                    all_ys += [y for y in ys if not np.isnan(y)]
                    g = geomean([y for y in ys if not np.isnan(y)])
                    if g:
                        titles.append(f"{v}: {g:.2f}×")
                ax.axhline(1, color=MUTED, linewidth=0.8, linestyle="--")
                ax.set_ylim(min(all_ys + [1]) / 1.3, max(all_ys + [1]) * 1.3)
                _log_axis(ax)
                _xlabels(ax, names)
                ax.set_title(suite + ("\n" + ", ".join(titles) if titles and len(others) <= 2 else ""))
            axes[0].set_ylabel(f"speedup over {exp.baseline}")
            if len(others) > 1:
                fig.legend(loc="upper center", ncol=len(others), bbox_to_anchor=(0.5, 1.08), frameon=False)
            fig.tight_layout()
            p = stem.with_name(stem.name + f"{tsuf}-speedup.pdf")
            fig.savefig(p, bbox_inches="tight"); fig.savefig(p.with_suffix(".png"), bbox_inches="tight")
            plt.close(fig)
            written.append(p)
    return written


def plot_scaling(exp: Experiment, summ: dict, stem: Path) -> list[Path]:
    """Speedup over 1 thread vs. thread count: thin line per benchmark, bold geomean per suite."""
    suites = _suites(summ)
    variants = [v.label for v in exp.variants]
    threads = sorted({k[3] for k in summ})
    written = []
    for v in variants:
        vsuf = f"-{v}" if len(variants) > 1 else ""
        fig, axes = plt.subplots(1, len(suites), figsize=(min(2.4 * len(suites), 14), 2.4), squeeze=False, sharey=True)
        axes = axes[0]
        suite_geo = {}
        for ax, (suite, names) in zip(axes, suites.items()):
            per_t: dict[int, list[float]] = {t: [] for t in threads}
            for n in names:
                base = summ.get((suite, n, v, threads[0]))
                if not base or base["status"] != "ok":
                    continue
                ys = []
                for t in threads:
                    s = summ.get((suite, n, v, t))
                    y = base["median"] / s["median"] if s and s["status"] == "ok" else np.nan
                    ys.append(y)
                    if not np.isnan(y):
                        per_t[t].append(y)
                ax.plot(threads, ys, color=MUTED, linewidth=0.6, alpha=0.6)
            geo = [geomean(per_t[t]) or np.nan for t in threads]
            suite_geo[suite] = geo
            ax.plot(threads, geo, color=PALETTE[0], linewidth=2, marker="o", markersize=3, label="geomean")
            ax.plot(threads, [t / threads[0] for t in threads], color=MUTED, linewidth=0.8, linestyle="--", label="ideal")
            ax.set_xscale("log", base=2)
            _log_axis(ax, "y", base=2)
            ax.set_xticks(threads)
            ax.set_xticklabels([str(t) for t in threads])
            ax.set_title(f"{suite} ({len(names)})")
            ax.set_xlabel("threads")
            ax.grid(True, axis="both")
        axes[0].set_ylabel(f"speedup over {threads[0]} thread")
        axes[0].legend(frameon=False, loc="upper left")
        fig.tight_layout()
        p = stem.with_name(stem.name + f"{vsuf}.pdf")
        fig.savefig(p, bbox_inches="tight"); fig.savefig(p.with_suffix(".png"), bbox_inches="tight")
        plt.close(fig)
        written.append(p)

        # one-panel summary: geomean per suite
        fig, ax = plt.subplots(figsize=(3.2, 2.4))
        for i, (suite, geo) in enumerate(suite_geo.items()):
            ax.plot(threads, geo, color=PALETTE[i % len(PALETTE)], linewidth=1.6, marker="o", markersize=3, label=suite)
        ax.plot(threads, [t / threads[0] for t in threads], color=MUTED, linewidth=0.8, linestyle="--", label="ideal")
        ax.set_xscale("log", base=2); _log_axis(ax, "y", base=2)
        ax.set_xticks(threads); ax.set_xticklabels([str(t) for t in threads])
        ax.set_xlabel("threads"); ax.set_ylabel(f"geomean speedup over {threads[0]} thread")
        ax.grid(True, axis="both")
        ax.legend(frameon=False)
        fig.tight_layout()
        p = stem.with_name(stem.name + f"{vsuf}-summary.pdf")
        fig.savefig(p, bbox_inches="tight"); fig.savefig(p.with_suffix(".png"), bbox_inches="tight")
        plt.close(fig)
        written.append(p)
    return written
