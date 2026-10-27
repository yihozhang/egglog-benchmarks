#!/usr/bin/env python3
"""Summarize completed real-world results separately for each baseline."""
from pathlib import Path
import csv
import hashlib
import json
import math
import statistics

ROOT = Path(__file__).resolve().parents[2]


def geomean(values):
    return math.exp(statistics.mean(map(math.log, values))) if values else None


def summarize(rows):
    report = {}
    for baseline in ['egglog-prior', 'egg']:
        selected = [r for r in rows if r['baseline'] == baseline]
        good = [r for r in selected if r['baseline_status'] == r['status'] == 'ok' and r['speedup']]
        suites = {}
        for suite in sorted({r['suite'] for r in good}):
            values = [float(r['speedup']) for r in good if r['suite'] == suite]
            suites[suite] = dict(n=len(values), geomean_speedup=geomean(values))
        report[baseline] = dict(
            total=len(selected), completed=len(good),
            benchmark_equal_geomean=geomean([float(r['speedup']) for r in good]),
            suites=suites,
            suite_equal_geomean=geomean([r['geomean_speedup'] for r in suites.values()]),
            table_size_differing_pairs=sum(int(r['table_size_mismatches']) > 0 for r in good),
        )
    return report


def main():
    source = ROOT / 'figures/paper/real-world.csv'
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert json.loads((source.parent / 'manifest.json').read_text())['files'][source.name] == source_sha
    report = summarize(list(csv.DictReader(source.open())))
    report['metadata'] = dict(
        source=str(source.relative_to(ROOT)),
        source_sha256=source_sha,
        selection='paper-cracked-20261025',
        egglog_commit=json.loads((ROOT / 'results/paper/base.json').read_text())['merged_commit'],
        no_decomp=True, threads=1,
        metric='baseline median wall time / cracked median wall time; three repetitions per configuration',
        suite_equal='geometric mean of the individual suite geometric means',
        benchmark_equal='geometric mean over every individual benchmark pair',
        caveats=['eggcc final table sizes differ across versions',
                 'STOREL uses backoff and node targets; actual target overshoots differ'],
    )
    (ROOT / 'results/paper/real-world-speedups.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
