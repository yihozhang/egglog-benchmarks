#!/usr/bin/env python3
"""Summarize preparation or execution speedups from a frozen no-decomp query run."""
from pathlib import Path
import argparse
import collections
import csv
import hashlib
import json
import math
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harness import query, results

HISTORICAL = Path('/home/yz489/egglog-benchmarks/results/query/griffin-refresh-20261020T0110Z/runs/20261020T033241958229Z-21d20d5ae98c-refresh-20261020T0110Z')
SELECTION = 'paper-cracked-20261025'


def geomean(values):
    return math.exp(statistics.mean(math.log(v) for v in values)) if values else None


def summarize(rows, keys, metric='optimize'):
    totals = {k: sum(r[k + '_' + metric] for r in rows) for k in keys}
    ratios = {k: [r[k + '_' + metric] / r[keys[0] + '_' + metric] for r in rows
                  if r[k + '_' + metric] > 0 and r[keys[0] + '_' + metric] > 0]
              for k in keys[1:]}
    total_key = 'optimization_totals_ms' if metric == 'optimize' else 'execution_totals_ms'
    return dict(queries=len(rows), **{total_key: totals},
                query_geomean_speedup={k: geomean(v) if len(v) == len(rows) else None for k, v in ratios.items()},
                query_arithmetic_mean_speedup={k: statistics.mean(v) if v and len(v) == len(rows) else None
                                               for k, v in ratios.items()},
                total_time_speedup={k: totals[k] / totals[keys[0]] if totals[keys[0]] > 0 else None
                                    for k in keys[1:]},
                queries_faster={k: sum(r[k + '_' + metric] > r[keys[0] + '_' + metric] for r in rows) for k in keys[1:]},
                undefined_ratio_queries={k: len(rows) - len(v) for k, v in ratios.items()})


def main(historical=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--historical', action='store_true', default=historical)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--metric', choices=['optimize', 'execute'], default='optimize')
    args = parser.parse_args()
    source = args.source or (HISTORICAL if args.historical else results.load_query_selection('query', SELECTION))
    manifest = json.loads((source / 'run.json').read_text())
    if args.historical:
        engine, stem = 'egglog_nd', 'historical-query-speedups'
    else:
        assert manifest['config']['no_decomp_only'] is True
        engine, stem = 'egglog', 'query-speedups'
    metric = args.metric
    if metric == 'execute':
        stem = stem.replace('query-speedups', 'query-runtime-speedups')
    keys = (engine, 'duckdb', 'umbra')
    rows = query.load_rows(source)  # verifies the immutable run bundle
    complete = [r for r in rows if all(r.get(k + '_status') == 'ok' for k in keys)]
    common = [r for r in complete if r.get(engine + '_rows', 0) > 0]
    assert all(r[k + '_' + metric] >= 0 for r in complete for k in keys)
    mismatches = [dict(program=r['program'], query=r['query'], counts={
        k: r[k + '_rows'] for k in keys if r.get(k + '_status') == 'ok'})
        for r in rows if len({r[k + '_rows'] for k in keys if r.get(k + '_status') == 'ok'}) > 1]
    programs = {p: summarize([r for r in common if r['program'] == p], keys, metric)
                for p in sorted({r['program'] for r in rows})}
    timing = ({engine: 'parse + typecheck + rule compilation',
               'duckdb': 'planner + optimizers + physical planner',
               'umbra': 'PREPARE, including planning and code generation'}
              if metric == 'optimize' else
              {engine: 'search + apply + merge; rebuild excluded',
               'duckdb': 'profiler latency minus planner, optimizer and physical planner time',
               'umbra': 'EXECUTE wall time after PREPARE'})
    timing_name = 'optimization' if metric == 'optimize' else 'execution'
    sql_reps = manifest['measurement']['sql']['reps']
    report = dict(
        source=str(source), run_id=manifest['run_id'],
        run_manifest_sha256=hashlib.sha256((source / 'run.json').read_bytes()).hexdigest(),
        egglog_commit=manifest['egglog_commit'], no_decomp=True,
        definition=f'speedup = database {timing_name} time / egglog no-decomp {timing_name} time; >1 favors egglog',
        timing_boundaries=timing,
        repetition_policy={engine: 'one pass', 'duckdb': f'median of {sql_reps} timed runs after one warmup',
                           'umbra': 'one PREPARE timing' if metric == 'optimize' else f'median of {sql_reps} timed EXECUTE runs after one warmup'},
        ratio_policy='Per-query means are null if any included query has a zero timing; total-time ratios remain defined when the summed egglog time is positive.',
        total_queries=len(rows), nonempty_queries=sum(r.get(engine + '_rows', 0) > 0 for r in rows),
        statuses={k: dict(collections.Counter(r.get(k + '_status', 'missing') for r in rows)) for k in keys},
        shared_successful_nonempty=summarize(common, keys, metric), programs=programs,
        shared_successful_including_empty=summarize(complete, keys, metric),
        model_equal_weight_geomean_of_total_time_speedups={
            k: geomean([p['total_time_speedup'][k] for p in programs.values()
                        if p['total_time_speedup'][k] is not None]) for k in keys[1:]},
        output_row_count_mismatches=len(mismatches),
        mismatched_queries=mismatches,
    )
    out = ROOT / 'results/paper'
    (out / f'{stem}.json').write_text(json.dumps(report, indent=2) + '\n')
    total_key = 'optimization_totals_ms' if metric == 'optimize' else 'execution_totals_ms'
    with (out / f'{stem}-programs.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['program', 'queries', 'egglog_' + metric + '_ms',
            'duckdb_' + metric + '_ms', 'umbra_' + metric + '_ms', 'duckdb_query_geomean_speedup',
            'umbra_query_geomean_speedup', 'duckdb_total_time_speedup', 'umbra_total_time_speedup'])
        writer.writeheader()
        for p, s in programs.items():
            writer.writerow(dict(program=p, queries=s['queries'], **{'egglog_' + metric + '_ms': s[total_key][engine]},
                **{k + '_' + metric + '_ms': s[total_key][k] for k in keys[1:]},
                **{k + '_query_geomean_speedup': s['query_geomean_speedup'][k] for k in keys[1:]},
                **{k + '_total_time_speedup': s['total_time_speedup'][k] for k in keys[1:]}))
    columns = ['program', 'query', 'common_nonempty'] + [k + '_' + field for k in keys for field in ('status', 'rows', metric)]
    with (out / f'{stem}-queries.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        ids = {(r['program'], r['query']) for r in common}
        writer.writerows(dict(r, common_nonempty=(r['program'], r['query']) in ids) for r in rows)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
