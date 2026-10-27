#!/usr/bin/env python3
"""Best measured thread configurations versus the best available prior baseline."""
from pathlib import Path
import collections
import copy
import csv
import hashlib
import json
import math
import statistics

ROOT = Path(__file__).resolve().parents[2]
FIGURES = ROOT / 'figures/paper'


def geomean(values):
    return math.exp(statistics.mean(map(math.log, values)))


def summarize(rows):
    suites = {}
    for suite in dict.fromkeys(r['suite'] for r in rows):
        selected = [r for r in rows if r['suite'] == suite]
        values = [r['speedup'] for r in selected]
        suites[suite] = dict(
            benchmarks=len(selected), geomean_speedup=geomean(values),
            min_speedup=min(values), max_speedup=max(values),
            selected_threads=dict(collections.Counter(r['cracked_threads'] for r in selected)),
        )
    return dict(
        benchmarks=len(rows), benchmark_equal_geomean=geomean([r['speedup'] for r in rows]),
        family_equal_geomean=geomean([s['geomean_speedup'] for s in suites.values()]),
        per_benchmark_range=[min(r['speedup'] for r in rows), max(r['speedup'] for r in rows)],
        family_geomean_range=[min(s['geomean_speedup'] for s in suites.values()),
                              max(s['geomean_speedup'] for s in suites.values())],
        benchmarks_faster=sum(r['speedup'] > 1 for r in rows), families=suites,
    )


def main():
    manifest = json.loads((FIGURES / 'manifest.json').read_text())
    inputs = {}

    def read(name):
        path = FIGURES / (name + '.csv')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert manifest['files'][path.name] == digest
        inputs[str(path.relative_to(ROOT))] = digest
        return list(csv.DictReader(path.open()))

    engines, world = read('engines'), read('real-world')
    scaling = {(r['suite'], r['benchmark']): r for r in read('scaling-real-world')}
    schedules = read('storel-schedule')
    targets = {r['benchmark']: int(r['node_limit']) for r in read('storel-backoff')}
    rows = []
    for suite, benchmark in dict.fromkeys((r['suite'], r['benchmark']) for r in engines):
        candidates = [r for r in engines if (r['suite'], r['benchmark']) == (suite, benchmark)
                      and r['status'] == 'ok']
        cracked = min((r for r in candidates if r['variant'].startswith('egglog-cracked')),
                      key=lambda r: float(r['median']))
        prior = min((r for r in candidates if not r['variant'].startswith('egglog-cracked')),
                    key=lambda r: float(r['median']))
        rows.append(dict(suite=suite, benchmark=benchmark, baseline=prior['variant'],
            baseline_threads=int(prior['threads']), baseline_median_s=float(prior['median']),
            cracked_threads=int(cracked['threads']), cracked_median_s=float(cracked['median']),
            cracked_source='engines', rewrite_batch=None,
            speedup=float(prior['median']) / float(cracked['median'])))
    for r in world:
        s = scaling[(r['suite'], r['benchmark'])]
        assert r['baseline_status'] == r['status'] == s['baseline_status'] == s['status'] == 'ok'
        times = {1: float(r['median_s']), 8: float(s['median_s'])}
        threads = min(times, key=times.get)
        source = ('storel' if r['suite'] == 'storel' else 'e2e') if threads == 1 else (
            'scaling-storel' if r['suite'] == 'storel' else 'scaling-real-world')
        rows.append(dict(suite=r['suite'], benchmark=r['benchmark'], baseline=r['baseline'],
            baseline_threads=int(r['baseline_threads']), baseline_median_s=float(r['baseline_median_s']),
            cracked_threads=threads, cracked_median_s=times[threads], cracked_source=source,
            rewrite_batch=1 if r['suite'] == 'storel' else None,
            speedup=float(r['baseline_median_s']) / times[threads]))
    assert len(rows) == len({(r['suite'], r['benchmark']) for r in rows}) == 94

    tuned = copy.deepcopy(rows)
    for r in tuned:
        if r['suite'] != 'storel':
            continue
        valid = [s for s in schedules if s['benchmark'] == r['benchmark']
                 and int(s['rewrite_batch']) > 1 and s['target_reached'] == 'True'
                 and s['status'] == 'ok' and int(s['repetitions_reaching_target']) == 3
                 and int(s['node_limit']) == targets[r['benchmark']]]
        if not valid:
            continue
        best = min(valid, key=lambda s: float(s['median_s']))
        if float(best['median_s']) < r['cracked_median_s']:
            r.update(cracked_median_s=float(best['median_s']), cracked_threads=int(best['threads']),
                     cracked_source='storel-schedule', rewrite_batch=int(best['rewrite_batch']))
            r['speedup'] = r['baseline_median_s'] / r['cracked_median_s']

    report = dict(
        selection='paper-cracked-20261025', source_csv_sha256=inputs, no_decomp=True,
        metric='fastest successful measured prior-system median / fastest measured cracked-configuration median',
        scope='94 unique paper instances: 7 algebraic and 87 real-world; database queries are reported separately',
        configuration_policy={
            'algebraic': 'best of cracked 1/4/8/16 threads; best of all prior systems and thread counts in engines.csv',
            'real_world': 'best of cracked 1/8 threads; egglog-prior for Hardboiled/Luminal/eggcc, egg backoff for STOREL',
            'repeated_configuration': '1-thread medians come from primary engine/e2e/STOREL comparisons; real-world 8-thread medians come from scaling runs; duplicate runs of one configuration are not separately minimized',
            'ablation': 'no-DVO ablation variants are excluded',
        },
        original_schedules=summarize(rows),
        including_valid_storel_schedule_tuning=summarize(tuned),
        caveats=[
            'Best prior means the best baseline actually measured on that benchmark; baseline coverage differs by suite.',
            'Thread selection is per benchmark, not a single global thread setting.',
            'The optional STOREL tuning admits only configurations with all three repetitions reaching their original node target; changing schedules can change the explored graph.',
            'eggcc table sizes can differ across versions/threads; STOREL target overshoots can differ.',
            'Ranges describe measured benchmark/family variation or explicitly different aggregation policies, not statistical confidence intervals.',
        ],
        benchmarks=rows, benchmarks_with_valid_storel_tuning=tuned,
    )
    out = ROOT / 'results/paper'
    (out / 'best-measured-speedups.json').write_text(json.dumps(report, indent=2) + '\n')
    with (out / 'best-measured-speedups.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['comparison', *rows[0]])
        writer.writeheader()
        for comparison, records in [('original_schedules', rows), ('with_valid_storel_tuning', tuned)]:
            writer.writerows(dict(comparison=comparison, **r) for r in records)
    print(json.dumps({k: report[k] for k in ['original_schedules', 'including_valid_storel_schedule_tuning']}, indent=2))


if __name__ == '__main__':
    main()
