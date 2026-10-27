#!/usr/bin/env python3
"""Refresh frozen-result figures and a readable report as the queue completes."""
from __future__ import annotations
import collections, csv, datetime, json, os, statistics, subprocess, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from harness import config, results, query
from query_speedups import summarize as summarize_query
from application_speedups import summarize as summarize_applications
EXPECTED=['scaling','scaling-real-world','e2e','storel','scaling-storel','storel-schedule','ablation','ablation-storel','engines','query']
SELECTION='paper-cracked-20261025'
def timestamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def atomic(path,value):
 temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)
def summarize(progress,status=None):
 lines=['# Paper run results','',f'Updated: {timestamp()}','',
        'The egglog-cracked measurements use the local merged base with `--no-decomp`. Baseline engines are pinned independently.']
 active=(status or {}).get('active',{})
 if active:
  if active.get('planned_configuration_groups'):
   lines+=['',f"Currently running **{active['experiment']}**: {active['completed_configuration_groups']}/{active['planned_configuration_groups']} configuration groups finished; {active['measurements_written']} measurements recorded; {active['skipped_repetitions_after_unsuccessful_outcomes']} subsequent repetitions skipped after an unsuccessful outcome."]
   if c:=active.get('next_configuration'):lines.append(f"Current/next case: `{c['suite']}/{c['benchmark']}`, `{c['variant']}`, {c['threads']} thread(s), repetition {c['next_rep']+1}/{c['reps']}.")
  else:lines+=['',f"Currently running **{active['experiment']}**; see the per-program logs for query progress."]
 lines+=['','| Experiment | State | Recorded measurements | Outcomes |', '|---|---|---:|---|']
 for name in EXPECTED:
  e=progress['experiments'].get(name,{})
  outcome_counts=active.get('outcomes',{}) if active.get('experiment')==name else e.get('outcomes',{})
  outcomes=', '.join(f'{k}: {v}' for k,v in outcome_counts.items())
  count=active['measurements_written'] if active.get('experiment')==name else e.get('measurements',e.get('queries','—'))
  lines.append(f"| {name} | {e.get('state','queued')} | {count} | {outcomes} |")
 lines+=['','`complete` means every planned configuration was attempted. Timeouts, OOMs, and failures remain explicit outcomes; they are not successful runtime measurements.','','## Figures','','| Comparison | PDF | Preview |','|---|---|---|']
 for label,stem in [('Algebraic engines','engines'),('Real-world engines','real-world'),('STOREL backoff time and work','storel-backoff'),('Algebraic scaling','scaling-algebraic'),('Real-world 8-thread scaling','scaling-real-world'),('STOREL schedule scaling','storel-schedule'),('STOREL schedule work','storel-schedule-work'),('Ablation: static size order','ablation'),('Ablation: static refinement order','ablation-refinement'),('Database query totals','query-totals'),('Database query scatter','query-scatter')]:
  if (ROOT/f'figures/paper/{stem}.pdf').exists():lines.append(f'| {label} | [PDF](../../figures/paper/{stem}.pdf) | [PNG](../../figures/paper/{stem}.png) |')
 lines+=['','## Completed real-world scaling','', '| Suite | 8-thread geometric mean speedup | Completed pairs |','|---|---:|---:|']
 for name in ['scaling-real-world','scaling-storel']:
  if progress['experiments'].get(name,{}).get('state')!='complete':continue
  selected,_=results.read_selection(name,SELECTION);exp=config.experiment_from_dict(name,selected['config']);rows=results.load_rows(exp,selection=SELECTION);s=results.summarize(rows)
  for suite in sorted({r['suite'] for r in rows}):
   ratios=[]
   for ss,b,v,t in s:
    a=s.get((ss,b,v,1));z=s.get((ss,b,v,8))
    if ss==suite and t==8 and a and z and a['status']==z['status']=='ok':ratios.append(a['median']/z['median'])
   g=results.geomean(ratios)
   lines.append(f'| {suite} | {g:.3f}× | {len(ratios)} |' if g else f'| {suite} | n/a | 0 |')
 for stem,title in [('real-world','Real-world runtime comparison'),('ablation','Ablation: full static size order'),('ablation-refinement','Ablation: full static refinement order')]:
  path=ROOT/f'figures/paper/{stem}-summary.csv'
  if not path.exists():continue
  lines+=['',f'## {title}','', '| Suite | Geometric mean speedup of cracked | Completed pairs | Pairs with different table sizes |','|---|---:|---:|---:|']
  for row in csv.DictReader(path.open()):
   speed=f"{float(row['geomean_speedup']):.3f}×" if row['geomean_speedup'] else 'n/a'
   lines.append(f"| {row['suite']} | {speed} | {row['completed_pairs']}/{row['total_pairs']} | {row['pairs_with_table_size_differences']} |")
  lines+=['',f'[Figure](../../figures/paper/{stem}.pdf); [per-benchmark details](../../figures/paper/{stem}-details.pdf); [CSV](../../figures/paper/{stem}.csv).']
  if stem=='real-world':
   lines+=['','Hardboiled, Luminal and eggcc use egglog-prior as baseline; STOREL uses egg with backoff.','', '| Baseline | Equal weight per application suite | Equal weight per benchmark | Completed benchmarks |','|---|---:|---:|---:|']
   aggregates=summarize_applications(list(csv.DictReader((ROOT/'figures/paper/real-world.csv').open())))
   for baseline,a in aggregates.items():
    lines.append(f"| {baseline} | {a['suite_equal_geomean']:.3f}× | {a['benchmark_equal_geomean']:.3f}× | {a['completed']}/{a['total']} |")
   lines+=['','The first aggregate averages the suite geometric means; the second weights all individual benchmark pairs equally. Both use ratios of three-run median wall times at one thread.']
  else:lines+=['','The named no-DVO baseline is 1.0×. Both figures share the same dynamic measurements. Incomplete pairs are excluded from the geometric mean and remain explicit in the CSV/details.']
 schedule=ROOT/'figures/paper/storel-schedule.csv'
 if schedule.exists():
  schedule_rows=list(csv.DictReader(schedule.open()))
  lines+=['','## STOREL schedule study','','Each rewrite phase runs K times before aux saturation, at K = 1, 2, 4, 8. Speedups include only runs that reach their node target.','', '| Rewrite batch | Geometric mean 8-thread speedup | Kernels reaching target at both thread counts |','|---:|---:|---:|']
  for k in [1,2,4,8]:
   vals=[float(r['speedup_over_same_batch_1t']) for r in schedule_rows if int(r['rewrite_batch'])==k and r['threads']=='8' and r['target_reached']=='True' and r['speedup_over_same_batch_1t'] not in ('','nan')]
   g=results.geomean(vals);speed=f'{g:.3f}×' if g else 'n/a'
   lines.append(f'| {k} | {speed} | {len(vals)}/6 |')
  incomplete=[r for r in schedule_rows if r['target_reached']!='True']
  if incomplete:
   lines+=['','The following configurations are excluded from speedups because at least one repetition stopped below target:','', '| Kernel | K | Threads | Repetitions reaching target | Reached nodes, min–max | Target |','|---|---:|---:|---:|---:|---:|']
   for r in incomplete:lines.append(f"| {r['benchmark']} | {r['rewrite_batch']} | {r['threads']} | {r['repetitions_reaching_target']}/{r['repetitions']} | {float(r['min_nodes']):,.0f}–{float(r['max_nodes']):,.0f} | {int(r['node_limit']):,} |")
  lines+=['','[Timing/scaling figure](../../figures/paper/storel-schedule.pdf); [reached-work figure](../../figures/paper/storel-schedule-work.pdf); [CSV](../../figures/paper/storel-schedule.csv). Schedule changes can change the explored e-graph even at similar reached sizes.']
 if progress['experiments'].get('engines',{}).get('state')=='complete':
  rows=list(csv.DictReader((ROOT/'figures/paper/engines.csv').open()))
  lines+=['','## Algebraic engine comparison','','[All seven workloads](../../figures/paper/engines.pdf); [CSV with all outcomes](../../figures/paper/engines.csv).','', '| Workload | cracked 1-thread median | cracked 16-thread median |','|---|---:|---:|']
  for benchmark in dict.fromkeys(r['benchmark'] for r in rows):
   cells=[]
   for variant in ['egglog-cracked','egglog-cracked-t16']:
    row=next(r for r in rows if r['benchmark']==benchmark and r['variant']==variant)
    cells.append(f"{float(row['median']):.4g} s" if row['status']=='ok' else row['status'])
   lines.append(f"| [{benchmark}](../../figures/paper/engines-{benchmark}.pdf) | {' | '.join(cells)} |")
  lines+=['','Against the single-thread oatlog baseline, the ratios below are oatlog median / cracked median. Values above 1 favor cracked. All times are medians of five runs.','', '| Workload | Cracked 1T | Cracked 4T | Cracked 8T | Cracked 16T |','|---|---:|---:|---:|---:|']
  speedups={t:[] for t in [1,4,8,16]}
  for benchmark in dict.fromkeys(r['benchmark'] for r in rows):
   base=next(r for r in rows if r['benchmark']==benchmark and r['variant']=='oatlog')
   cells=[]
   for t in speedups:
    v='egglog-cracked' if t==1 else f'egglog-cracked-t{t}'
    r=next(r for r in rows if r['benchmark']==benchmark and r['variant']==v)
    if base['status']==r['status']=='ok':
     ratio=float(base['median'])/float(r['median']);speedups[t].append(ratio);cells.append(f'{ratio:.3f}×')
    else:cells.append('n/a')
   lines.append(f"| {benchmark} | {' | '.join(cells)} |")
  cells=[f'{results.geomean(v):.3f}×' if v else 'n/a' for v in speedups.values()]
  lines.append(f"| Geometric mean | {' | '.join(cells)} |")
 if progress['experiments'].get('query',{}).get('state')=='complete':
  rd=ROOT/'runs'/progress['experiments']['query']['run_id'];rows=query.load_rows(rd);systems=query.present(rows)
  common=[r for r in rows if r.get('egglog_rows',0)>0 and all(r.get(f'{k}_status')=='ok' for k,_ in systems)]
  mismatches=sum(len({r[f'{k}_rows'] for k,_ in systems if r.get(f'{k}_status')=='ok'})>1 for r in rows)
  lines+=['','## Database query comparison','',f'{len(rows)} translated queries; {len(common)} nonempty queries completed by all three engines; {mismatches} output row-count mismatches. Totals below cover only that shared nonempty subset.','', '| Engine | Optimize/compile total | Execute total | Unsuccessful queries |','|---|---:|---:|---:|']
  for k,label in systems:
   failures=sum(r.get(f'{k}_status')!='ok' for r in rows)
   lines.append(f"| {label} | {sum(r[f'{k}_optimize'] for r in common):.2f} ms | {sum(r[f'{k}_execute'] for r in common):.2f} ms | {failures} |")
  keys=('egglog','duckdb','umbra')
  lines+=['','Optimization speedup is database preparation time divided by cracked preparation time; values above 1 favor cracked. Geometric means below weight each shared successful nonempty query equally.','', '| Program | Shared nonempty queries | Geometric mean vs DuckDB | Geometric mean vs Umbra |','|---|---:|---:|---:|']
  for program in sorted({r['program'] for r in rows})+['All queries']:
   subset=common if program=='All queries' else [r for r in common if r['program']==program]
   summary=summarize_query(subset,keys)
   cells=[f'{summary["query_geomean_speedup"][k]:.3f}×' if summary['query_geomean_speedup'][k] else 'n/a' for k in keys[1:]]
   lines.append(f"| {program} | {len(subset)} | {' | '.join(cells)} |")
  totals=summarize_query(common,keys)['total_time_speedup']
  if common:lines+=['',f"The ratios of total optimization times over the same queries are **{totals['duckdb']:.3f}× vs DuckDB** and **{totals['umbra']:.3f}× vs Umbra**. These are distinct from the per-query geometric means."]
  runtime=summarize_query(common,keys,'execute')
  if common and all(runtime['query_geomean_speedup'][k] is not None for k in keys[1:]):
   g=runtime['query_geomean_speedup'];t=runtime['total_time_speedup'];wins=runtime['queries_faster']
   lines+=['',f"For **execution time**, excluding preparation, the per-query geometric means are **{g['duckdb']:.3f}× vs DuckDB** and **{g['umbra']:.3f}× vs Umbra**; the total-time ratios are **{t['duckdb']:.3f}× / {t['umbra']:.3f}×**. Cracked is faster on {wins['duckdb']}/{len(common)} and {wins['umbra']}/{len(common)} queries, respectively. Egglog execution is search + apply + merge, excluding rebuild."]
  lines+=['','For the per-model bars, another useful aggregate weights each model equally: compute each model’s total-time ratio, then take the geometric mean across models. This differs from weighting each query equally.','', '| Metric | Geometric mean of model total-time ratios vs DuckDB | vs Umbra | Models |','|---|---:|---:|---:|']
  for metric,label in [('optimize','Optimize/compile'),('execute','Execute')]:
   model_summaries=[summarize_query([r for r in common if r['program']==p],keys,metric) for p in sorted({r['program'] for r in common})]
   cells=[]
   for k in keys[1:]:
    g=results.geomean([s['total_time_speedup'][k] for s in model_summaries if s['total_time_speedup'][k] is not None])
    cells.append(f'{g:.3f}×' if g else 'n/a')
   lines.append(f"| {label} | {' | '.join(cells)} | {len(model_summaries)} |")
  lines+=['','[Totals](../../figures/paper/query-totals.pdf); [per-query scatter](../../figures/paper/query-scatter.pdf); [CSV](../../figures/paper/query.csv). Timing boundaries differ by engine as specified in the run guide.']
 lines+=['','## Result-size checks','','The real-world comparisons measure the original schedules. All 32 eggcc programs have cross-version table-size differences; those same programs differed in the historical comparison. In cracked-only 1/8-thread measurements, 24 eggcc programs vary in table sizes. These measurements do not establish identical work or extracted-program equivalence. STOREL is node-limited and can overshoot its target differently. See the [guide](README.md) and [audit](audit.json) for per-experiment evidence.']
 lines+=['','## Files','',
         '- [Run configuration and interpretation](README.md)',
         '- [Live status](status.json)',
         '- [Settings and size audit](audit.json)',
         '- [Figure checksums](figures.json)',
         '- [Real-world aggregates by baseline and weighting](real-world-speedups.json)',
         '- [Oatlog comparison by thread count](oatlog-speedups.json)',
         '- [Fresh database preparation speedups](query-speedups.json)',
         '- [Fresh database execution speedups](query-runtime-speedups.json)',
         '- [Historical database preparation speedups](historical-query-speedups.json)',
         '- [Historical database execution speedups](historical-query-runtime-speedups.json)',
         '- Figures and CSVs: `../../figures/paper/`',
         '- Individual run logs: `../../runs/<run-id>/logs/`',
         '- Queue/build/check logs: `../../logs/paper/`','']
 report_path=ROOT/'results/paper/RESULTS.md';temp=report_path.with_suffix('.tmp')
 temp.write_text('\n'.join(lines));temp.replace(report_path)

def main():
 os.chdir(ROOT);last=set()
 while True:
  p=ROOT/'results/paper/progress.json'
  if not p.exists():time.sleep(5);continue
  progress=json.loads(p.read_text());completed={n for n,e in progress['experiments'].items() if e['state']=='complete'}
  status={'updated_at':timestamp(),'completed_experiments':sorted(completed),'queue':progress['experiments']}
  for path in sorted((ROOT/'runs').glob('*paper-final/run.json')):
   m=json.loads(path.read_text())
   if m['state']=='running':
    measurements=path.parent/'measurements.jsonl'
    rows=[json.loads(l) for l in measurements.read_text().splitlines()] if measurements.exists() else []
    cases=m.get('cases',[])
    status['active']={'experiment':m['experiment'],'run_id':m['run_id'],'measurements_written':len(rows),'outcomes':dict(collections.Counter(r['status'] for r in rows)),'planned_configuration_groups':len(cases),'planned_measurements':sum(c.get('reps',1) for c in cases)}
    groups=collections.defaultdict(list)
    key=lambda r:tuple(r[k] for k in ['suite','benchmark','variant','threads'])
    for r in rows:groups[key(r)].append(r)
    finished=skipped=0
    for c in cases:
     observed=groups[key(c)]
     done=len(observed)==c['reps'] or any(r['status']!='ok' for r in observed)
     if done:finished+=1;skipped+=c['reps']-len(observed)
     elif 'next_configuration' not in status['active']:status['active']['next_configuration']={**c,'next_rep':len(observed)}
    status['active'].update(completed_configuration_groups=finished,skipped_repetitions_after_unsuccessful_outcomes=skipped)
    if rows:status['active']['last_measurement']={k:rows[-1].get(k) for k in ['suite','benchmark','variant','threads','rep','status','wall_s']}
  atomic(ROOT/'results/paper/status.json',status)
  if completed!=last:
   for task in ['plot','audit']:
    with (ROOT/f'logs/paper/{task}.log').open('w') as log:
     subprocess.run([sys.executable,f'scripts/paper/{task}.py'],stdout=log,stderr=subprocess.STDOUT,check=True)
   last=completed
   print(timestamp(),'Updated',sorted(completed),flush=True)
  summarize(progress,status)
  if all(progress['experiments'].get(n,{}).get('state') in ('complete','failed','interrupted') for n in EXPECTED):
   if all(progress['experiments'][n]['state']=='complete' for n in EXPECTED):
    with (ROOT/'logs/paper/final-verification.log').open('w') as log:
     subprocess.run([sys.executable,'scripts/paper/verify.py'],stdout=log,stderr=subprocess.STDOUT,check=True)
    print(timestamp(),'Final artifact verification passed.',flush=True)
   print(timestamp(),'Queue reached terminal states.',flush=True);return
  time.sleep(45)
if __name__=='__main__':main()
