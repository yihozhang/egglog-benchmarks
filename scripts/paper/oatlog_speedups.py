#!/usr/bin/env python3
"""Compare completed cracked thread configurations with single-thread oatlog."""
from pathlib import Path
import csv
import hashlib
import json
import math
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harness import artifacts

VARIANTS = {1: 'egglog-cracked', 4: 'egglog-cracked-t4',
            8: 'egglog-cracked-t8', 16: 'egglog-cracked-t16'}


def main():
    run_id = json.loads((ROOT / 'results/paper/engine-rerun.json').read_text())['replacement_run_id']
    run = ROOT / 'runs' / run_id
    manifest = json.loads((run / 'run.json').read_text())
    if manifest['state'] == 'complete':
        artifacts.verify_run(run)
    rows = [json.loads(line) for line in (run / 'measurements.jsonl').read_text().splitlines()]
    benchmarks = list(dict.fromkeys((r['suite'], r['benchmark']) for r in rows))
    assert len(benchmarks) == 7
    records, raw = [], []
    for suite, benchmark in benchmarks:
        groups = {v: [r for r in rows if (r['suite'], r['benchmark'], r['variant']) == (suite, benchmark, v)]
                  for v in ['oatlog', *VARIANTS.values()]}
        assert all(len(rs) == 5 and all(r['status'] == 'ok' for r in rs) for rs in groups.values()), benchmark
        assert all(r['threads'] == 1 for r in groups['oatlog'])
        assert all('--no-decomp' in r['flags'] and r['threads'] == t
                   for t, v in VARIANTS.items() for r in groups[v])
        times = {v: statistics.median(r['wall_s'] for r in rs) for v, rs in groups.items()}
        for rs in groups.values():
            raw.extend(rs)
        records.append(dict(suite=suite, benchmark=benchmark, oatlog_1t_median_s=times['oatlog'],
            cracked_median_s={str(t): times[v] for t, v in VARIANTS.items()},
            speedup_vs_oatlog={str(t): times['oatlog'] / times[v] for t, v in VARIANTS.items()}))
    gm = {str(t): math.exp(statistics.mean(math.log(r['speedup_vs_oatlog'][str(t)]) for r in records))
          for t in VARIANTS}
    raw_text = ''.join(json.dumps(r, sort_keys=True) + '\n' for r in raw)
    report = dict(source_run=str(run.relative_to(ROOT)), source_run_state=manifest['state'],
        relevant_configurations_complete=True, baseline='oatlog, 1 thread',
        metric='oatlog 1-thread median wall time / cracked N-thread median wall time; >1 favors cracked',
        repetitions=5, no_decomp=True, benchmarks=records, geometric_mean_over_seven_workloads=gm,
        relevant_measurements=len(raw), relevant_measurements_sha256=hashlib.sha256(raw_text.encode()).hexdigest(),
        commits={v: sorted({r['commit'] for r in raw if r['variant'] == v}) for v in ['oatlog', *VARIANTS.values()]})
    out = ROOT / 'results/paper'
    (out / 'oatlog-speedups.json').write_text(json.dumps(report, indent=2) + '\n')
    (out / 'oatlog-speedups-measurements.jsonl').write_text(raw_text)
    with (out / 'oatlog-speedups.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['suite', 'benchmark', 'cracked_threads',
            'oatlog_1t_median_s', 'cracked_median_s', 'speedup'])
        writer.writeheader()
        for r in records:
            for t in VARIANTS:
                writer.writerow(dict(suite=r['suite'], benchmark=r['benchmark'], cracked_threads=t,
                    oatlog_1t_median_s=r['oatlog_1t_median_s'], cracked_median_s=r['cracked_median_s'][str(t)],
                    speedup=r['speedup_vs_oatlog'][str(t)]))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
