#!/usr/bin/env python3
"""Audit frozen paper rows, required settings, result sizes, and reached targets."""
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from harness import artifacts, config, results, query
progress=json.loads((ROOT/'results/paper/progress.json').read_text());report={}
for name,entry in progress['experiments'].items():
 if entry['state']!='complete':report[name]={'state':entry['state']};continue
 rd=ROOT/'runs'/entry['run_id'];manifest=artifacts.verify_run(rd)
 if name=='query':
  assert manifest['config']['no_decomp_only']
  for program in manifest['programs']:
   for line in (rd/program/'egglog.jsonl').read_text().splitlines():
    r=json.loads(line);assert r['no_decomp'] is True and r['engine_label']=='egglog-cracked'
  report[name]={'queries':len(query.load_rows(rd)),'no_decomp_only':True};continue
 exp,rows=results.report_data(name,entry['run_id']);summary=results.summarize(rows)
 for r in rows:
  if r['system'].startswith('egglog') and r['variant']!='egglog-prior':assert '--no-decomp' in r['flags']
  if r['variant'].startswith('no-DVO'):assert '--no-dynamic-order' in r['flags']
  if r['suite']=='storel':assert r['variant']!='egglog-prior'
 mismatches=[];uncheckable=[];checked_pairs=0
 for s,b in dict.fromkeys((r['suite'],r['benchmark']) for r in rows):
  if s not in ['math-microbenchmark','horner','matmul-assoc']:continue
  compared=[r for r in rows if r['suite']==s and r['benchmark']==b and r['status']=='ok']
  if not compared:continue
  base=compared[0]
  for r in compared[1:]:
   left=base['report'].get('sizes',{});right=r['report'].get('sizes',{});common=left.keys()&right.keys()
   if not common:uncheckable.append({'benchmark':b,'variant':r['variant'],'threads':r['threads'],'rep':r['rep']})
   else:checked_pairs+=1
   diff={k:[left[k],right[k]] for k in common if left[k]!=right[k]}
   if diff:mismatches.append({'benchmark':b,'variant':r['variant'],'threads':r['threads'],'rep':r['rep'],'differences':diff})
 variable_sizes=[]
 for suite,benchmark in dict.fromkeys((r['suite'],r['benchmark']) for r in rows):
  rs=[r for r in rows if r['suite']==suite and r['benchmark']==benchmark and r['status']=='ok' and r.get('report',{}).get('sizes')]
  if len(rs)<2:continue
  common=set.intersection(*(set(r['report']['sizes']) for r in rs))
  changed=[table for table in sorted(common) if len({r['report']['sizes'][table] for r in rs})>1]
  if changed:variable_sizes.append({'suite':suite,'benchmark':benchmark,'differing_tables':changed})
 below_target=[{k:r[k] for k in ['benchmark','variant','threads','rep','wall_s']} | {k:r['report'].get(k) for k in ['nodes','node_limit','iterations','stop_reason']} for r in rows if r['suite']=='storel' and r['status']=='ok' and r['report'].get('stop_reason')!='node-limit']
 report[name]={'measurements':len(rows),'groups':len(summary),'statuses':{s:sum(r['status']==s for r in rows) for s in sorted({r['status'] for r in rows})},'algebraic_size_pairs_with_common_tables':checked_pairs,'algebraic_size_pairs_without_common_tables':uncheckable,'algebraic_size_mismatches':mismatches,'table_sizes_vary_across_variants_threads_or_repetitions':variable_sizes,'storel_below_target_runs':below_target,'harness_check_exit_code':entry.get('check_exit_code'),'all_cracked_no_decomp':True}
(ROOT/'results/paper/audit.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
