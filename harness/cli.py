"""Run benchmarks, inspect portable runs, and select reproducible result sets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import artifacts, config, query, results, runner


def cmd_list(args):
    if args.experiment and query.is_query(args.experiment):
        cfg = query.load(args.experiment)
        print(f"{args.experiment} (query): {cfg['description']}")
        print(f"  suite={cfg['suite']} egglog@{cfg['egglog_ref']} engines={cfg.get('engines')} threads={cfg.get('threads', 1)} "
              f"reps={cfg.get('reps', 3)} timeout={cfg.get('timeout', 60)}s")
        print(f"  programs: {' '.join(query.programs_of(cfg))}")
    elif args.experiment:
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
            desc = query.load(name)["description"] if query.is_query(name) else config.load_experiment(name).description
            print(f"  {name:12s} {desc}")
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
            if d.is_dir() and not d.name.startswith("_"):
                if (d / "run").exists():
                    notes = config.load_system(d.name).notes
                else:  # not a regular adapter (e.g. egglog-query)
                    notes = config.tomllib.loads((d / "system.toml").read_text()).get("notes", "").strip()
                print(f"  {d.name}")
                for line in notes.splitlines():
                    print(f"      {line}")


def cmd_build(args):
    b = runner.build(args.system, args.ref, tuple(args.patches))
    print(b.dir)
    for k, v in b.info.items():
        print(f"  {k}: {v}")


def cmd_run(args):
    if query.is_query(args.experiment):
        progs = [b.split("/")[-1] for b in args.benchmarks or []] or None
        query.run(args.experiment, programs=progs, engines=args.variants or None, tag=args.tag,
                  machine=args.machine, cpus=args.cpus, threads=args.threads, reps=args.reps,
                  timeout=args.timeout, memory_gb=args.memory_gb, dry_run=args.dry_run)
        return
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
        machine=args.machine,
        cpus=args.cpus,
    )



def _query_report(args):
    if args.selection:
        selected, _ = results.read_selection(Path(args.experiment).stem, args.selection)
        return selected["kind"] == "query"
    name = config.experiment_path(args.experiment).stem
    run = args.run or "latest"
    bundles = [rd for rd in artifacts.list_runs(name)
               if artifacts.read_manifest(rd).get("state") == "complete"]
    if bundles and run == "latest":
        return artifacts.read_manifest(bundles[-1])["kind"] == "query"
    if run not in ("latest", "all"):
        try:
            return artifacts.read_manifest(artifacts.resolve_run(run, name))["kind"] == "query"
        except ValueError:
            pass
    return query.is_query(args.experiment)


def _report_data(args):
    if args.selection:
        name = Path(args.experiment).stem
        selected, _ = results.read_selection(name, args.selection)
        cfg = {**selected["config"], "metric": selected["metric"]}
        exp = config.experiment_from_dict(name, cfg)
        return exp, results.load_rows(exp, selection=args.selection)
    return results.report_data(args.experiment, args.run or "latest")


def cmd_summary(args):
    if _query_report(args):
        return query.summary(config.experiment_path(args.experiment).stem, args.run or "latest", selection=args.selection)
    exp, rows = _report_data(args)
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
    if _query_report(args):
        return query.check(config.experiment_path(args.experiment).stem, args.run or "latest", selection=args.selection)
    exp, rows = _report_data(args)
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

    if _query_report(args):
        for p in query.plot(config.experiment_path(args.experiment).stem, args.run or "latest", args.out, selection=args.selection):
            print(p)
        return
    exp, rows = _report_data(args)
    out_dir = config.FIGURES if args.out is None else args.out
    for p in plot.plot_experiment(exp, rows, out_dir, metric=args.metric or exp.metric):
        print(p)



def cmd_runs(args):
    for rd in artifacts.list_runs(args.experiment):
        m = artifacts.read_manifest(rd)
        print(f"{m['run_id']:45s} {m['experiment']:22s} {m['state']:12s} {m['machine_id']}")


def cmd_inspect(args):
    rd = artifacts.resolve_run(args.run_id)
    manifest = artifacts.read_manifest(rd)
    if manifest.get("state") != "running":
        artifacts.verify_run(rd)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"bundle: {rd}")


def cmd_select(args):
    directory = results.select_runs(args.experiment, args.runs, args.name,
                                    allow_legacy=args.allow_legacy, metric=args.metric)
    print(f"selected results: {directory}")
    print("Review these files and commit them with Git when ready.")


def cmd_export(args):
    print(artifacts.export_run(args.run_id, args.out))


def cmd_import(args):
    print(artifacts.import_run(args.archive))


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

    p = sub.add_parser("run", help="run a named experiment or TOML path into an isolated runs/ bundle")
    p.add_argument("experiment")
    p.add_argument("-b", "--benchmarks", nargs="*", metavar="SUITE[/NAME]", help="restrict to these suites/benchmarks (globs ok)")
    p.add_argument("-v", "--variants", nargs="*", metavar="LABEL", help="restrict to these variants")
    p.add_argument("-t", "--threads", nargs="*", type=int)
    p.add_argument("-r", "--reps", type=int)
    p.add_argument("--timeout", type=float, help="seconds per run")
    p.add_argument("--memory-gb", type=float, help="memory cap per run in GB (0 = none)")
    p.add_argument("--tag", help="suffix for the run id")
    p.add_argument("--machine", help="machine alias recorded with detected hardware (defaults to hostname)")
    p.add_argument("--cpus", help="override CPU affinity for this machine, e.g. 0-15 (e2e only)")
    p.add_argument("-n", "--dry-run", action="store_true", help="print the matrix and exit")
    p.set_defaults(fn=cmd_run)

    for name, fn, help_ in [
        ("summary", cmd_summary, "print median times and geomean speedups"),
        ("check", cmd_check, "check that all variants reach the same table sizes"),
        ("plot", cmd_plot, "write figures to figures/ (or --out)"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("experiment")
        source = p.add_mutually_exclusive_group()
        source.add_argument("--run", help="'latest' complete run (default), exact ID, unique prefix/tag, or 'all' compatible disjoint runs")
        source.add_argument("--selection", help="explicit committed result selection (uses its frozen configuration)")
        if name != "check":
            p.add_argument("--metric", help="wall_s or report.<field>, e.g. report.run_s (default: the experiment's `metric`)")
        if name == "plot":
            p.add_argument("--out", type=__import__("pathlib").Path, help="output directory (e.g. the paper's figures/)")
        p.set_defaults(fn=fn)

    p = sub.add_parser("runs", help="list local and imported run bundles")
    p.add_argument("experiment", nargs="?")
    p.set_defaults(fn=cmd_runs)

    p = sub.add_parser("inspect", help="inspect run metadata and verify completed bundle integrity")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("select", help="freeze selected measurements and provenance under results/ for Git review")
    p.add_argument("experiment")
    p.add_argument("--runs", nargs="+", required=True, metavar="ID")
    p.add_argument("--as", dest="name", required=True, help="new selection name, e.g. paper-v1")
    p.add_argument("--allow-legacy", action="store_true", help="explicitly accept incomplete provenance in old results")
    p.add_argument("--metric", help="freeze an analysis metric (default: experiment metric)")
    p.set_defaults(fn=cmd_select)

    p = sub.add_parser("export", help="pack a sealed run with checksums for transfer to another machine")
    p.add_argument("run_id")
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("import", help="verify and import a run archive; identical imports are harmless")
    p.add_argument("archive", type=Path)
    p.set_defaults(fn=cmd_import)

    args = ap.parse_args(argv)
    try:
        rc = args.fn(args)
    except (ValueError, OSError) as exc:
        ap.exit(1, f"bench: {exc}\n")
    sys.exit(rc or 0)
