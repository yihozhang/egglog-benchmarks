"""`bench` command line: list / build / run / summary / check / plot."""

from __future__ import annotations

import argparse
import sys

from . import config, results, runner


def cmd_list(args):
    if args.experiment:
        exp = config.load_experiment(args.experiment)
        print(f"{exp.name}: {exp.description}")
        print(f"  threads={exp.threads} reps={exp.reps} timeout={exp.timeout:g}s memory={exp.memory_gb:g}GB cpus={exp.cpus} plot={exp.plot} baseline={exp.baseline}")
        for v in exp.variants:
            patches = f" +{'+'.join(v.patches)}" if v.patches else ""
            print(f"  variant {v.label:28s} {v.system}@{v.ref}{patches} {' '.join(v.flags)}")
        benches = config.resolve_benchmarks(exp.benchmarks)
        systems = {v.system: config.load_system(v.system) for v in exp.variants}
        print(f"  {len(benches)} benchmarks:")
        for b in benches:
            unsupported = [s for s, sy in systems.items() if not sy.supports(b)]
            note = f"  (unsupported by {', '.join(unsupported)})" if unsupported else ""
            print(f"    {b.id}{note}")
    else:
        print("experiments:")
        for name in config.list_experiments():
            print(f"  {name:12s} {config.load_experiment(name).description}")
        print("suites:")
        for d in sorted(config.BENCHMARKS.iterdir()):
            if d.is_dir():
                try:
                    n = len(config.load_suite(d.name))
                except SystemExit as e:
                    n = f"? ({e})"
                print(f"  {d.name:20s} {n} benchmarks")
        print("systems:")
        for d in sorted(config.SYSTEMS.iterdir()):
            if d.is_dir():
                print(f"  {d.name}")


def cmd_build(args):
    b = runner.build(args.system, args.ref, tuple(args.patches))
    print(b.dir)
    for k, v in b.info.items():
        print(f"  {k}: {v}")


def cmd_run(args):
    exp = config.load_experiment(args.experiment)
    runner.run_experiment(
        exp,
        only_benchmarks=args.benchmarks or None,
        only_variants=args.variants or None,
        threads=args.threads or None,
        reps=args.reps,
        timeout=args.timeout,
        memory_gb=args.memory_gb,
        dry_run=args.dry_run,
        tag=args.tag,
    )


def cmd_summary(args):
    exp = config.load_experiment(args.experiment)
    rows = results.load_rows(exp, args.run)
    metric = args.metric or exp.metric
    summ = results.summarize(rows, metric)
    variants = [v.label for v in exp.variants]
    benches = sorted({(k[0], k[1]) for k in summ})
    print(f"metric: {metric}")
    # a variant pinned to one thread count (e.g. "egglog-t8") is a column of its own
    fixed = {v.label: v.threads[0] for v in exp.variants if v.threads and len(v.threads) == 1}
    threads = sorted({k[3] for k in summ if k[2] not in fixed}) or [1]

    def cell(suite, name, v, t):
        s = summ.get((suite, name, v, fixed.get(v, t)))
        if s is None:
            return "-", None
        if s["status"] != "ok":
            return s["status"], None
        return f"{s['median']:.3f}", s["median"]

    if len(threads) > 1:
        # scaling layout: one block per variant, columns are thread counts, speedup vs. the first
        for v in variants:
            print(f"== {v} ({metric}, median of reps; speedup over {threads[0]} thread in parentheses)")
            print(f"{'benchmark':42s}" + "".join(f"{'t=' + str(t):>18s}" for t in threads))
            per_t: dict[int, list[float]] = {t: [] for t in threads}
            for suite, name in benches:
                base = cell(suite, name, v, threads[0])[1]
                line = f"{suite + '/' + name:42s}"
                for t in threads:
                    txt, val = cell(suite, name, v, t)
                    if val and base:
                        per_t[t].append(base / val)
                        txt += f" ({base / val:.1f}x)"
                    line += f"{txt:>18s}"
                print(line)
            print(f"{'geomean speedup':42s}" + "".join(
                f"{(f'{results.geomean(per_t[t]):.2f}x' if per_t[t] else '-'):>18s}" for t in threads))
        return

    t = threads[0]
    print(f"{'benchmark':42s}" + "".join(f"{v:>22s}" for v in variants))
    for suite, name in benches:
        print(f"{suite + '/' + name:42s}" + "".join(f"{cell(suite, name, v, t)[0]:>22s}" for v in variants))
    if exp.baseline:
        print(f"geomean speedup vs {exp.baseline}:")
        for suite in sorted({s for s, _ in benches}):
            line = f"  {suite:20s}"
            for v in variants:
                if v == exp.baseline:
                    continue
                ratios = []
                for suite2, name in benches:
                    if suite2 != suite:
                        continue
                    base = cell(suite, name, exp.baseline, t)[1]
                    val = cell(suite, name, v, t)[1]
                    if base and val:
                        ratios.append(base / val)
                g = results.geomean(ratios)
                line += f"  {v}: {g:.2f}x (n={len(ratios)})" if g else f"  {v}: n/a"
            print(line)


def cmd_check(args):
    """Compare final table sizes across variants: equal sizes mean the variants did the same work."""
    exp = config.load_experiment(args.experiment)
    rows = results.load_rows(exp, args.run)
    summ = results.summarize(rows)
    by_bench: dict[tuple, dict[str, dict]] = {}
    for (suite, name, variant, t), s in summ.items():
        if s["status"] == "ok" and s["sizes"]:
            by_bench.setdefault((suite, name, t), {})[variant] = s["sizes"]
    bad = 0
    for (suite, name, t), by_v in sorted(by_bench.items()):
        labels = list(by_v)
        ref = by_v[labels[0]]
        for l in labels[1:]:
            other = by_v[l]
            common = set(ref) & set(other)
            diff = {k: (ref[k], other[k]) for k in common if ref[k] != other[k]}
            if diff:
                bad += 1
                shown = ", ".join(f"{k}: {a} vs {b}" for k, (a, b) in list(diff.items())[:5])
                print(f"MISMATCH {suite}/{name} t={t} {labels[0]} vs {l}: {shown}")
    print(f"{len(by_bench)} configurations compared, {bad} mismatches")
    return 1 if bad else 0


def cmd_plot(args):
    from . import plot  # matplotlib is imported lazily

    exp = config.load_experiment(args.experiment)
    rows = results.load_rows(exp, args.run)
    out_dir = config.FIGURES if args.out is None else args.out
    for p in plot.plot_experiment(exp, rows, out_dir, metric=args.metric or exp.metric):
        print(p)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list experiments, suites and systems (or one experiment's matrix)")
    p.add_argument("experiment", nargs="?")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("build", help="build a system at a git ref (optionally with patches)")
    p.add_argument("system")
    p.add_argument("ref")
    p.add_argument("patches", nargs="*", help="names of systems/<system>/patches/<name>.patch")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("run", help="run an experiment (appends to results/<experiment>/)")
    p.add_argument("experiment")
    p.add_argument("-b", "--benchmarks", nargs="*", metavar="SUITE[/NAME]", help="restrict to these suites/benchmarks (globs ok)")
    p.add_argument("-v", "--variants", nargs="*", metavar="LABEL", help="restrict to these variants")
    p.add_argument("-t", "--threads", nargs="*", type=int)
    p.add_argument("-r", "--reps", type=int)
    p.add_argument("--timeout", type=float, help="seconds per run")
    p.add_argument("--memory-gb", type=float, help="memory cap per run in GB (0 = none)")
    p.add_argument("--tag", help="suffix for the run id")
    p.add_argument("-n", "--dry-run", action="store_true", help="print the matrix and exit")
    p.set_defaults(fn=cmd_run)

    for name, fn, help_ in [
        ("summary", cmd_summary, "print median times and geomean speedups"),
        ("check", cmd_check, "check that all variants reach the same table sizes"),
        ("plot", cmd_plot, "write figures to figures/ (or --out)"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("experiment")
        p.add_argument("--run", default="latest", help="'latest' (default), 'all', or a run id prefix")
        if name != "check":
            p.add_argument("--metric", help="wall_s or report.<field>, e.g. report.run_s (default: the experiment's `metric`)")
        if name == "plot":
            p.add_argument("--out", type=__import__("pathlib").Path, help="output directory (e.g. the paper's figures/)")
        p.set_defaults(fn=fn)

    args = ap.parse_args(argv)
    rc = args.fn(args)
    sys.exit(rc or 0)
