#!/usr/bin/env python3
"""Sequential, checkpointed execution of the no-decomposition paper experiments."""
from __future__ import annotations
import argparse, contextlib, datetime, json, os, subprocess, sys, traceback
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harness import artifacts, config, query, results, runner
EXPERIMENTS = ['scaling', 'scaling-real-world', 'e2e', 'storel', 'scaling-storel',
               'storel-schedule', 'ablation', 'ablation-storel', 'engines', 'query']
PROGRESS = ROOT / 'results/paper/progress.json'
SELECTION = 'paper-cracked-20261025'
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def save(progress):
    temp=PROGRESS.with_suffix('.tmp'); temp.write_text(json.dumps(progress,indent=2)+'\n'); temp.replace(PROGRESS)
def audit(exp):
    if query.is_query(exp):
        assert query.load(exp).get('no_decomp_only') is True
        return
    e=config.load_experiment(exp)
    for v in e.variants:
        if v.system.startswith('egglog') and v.label != 'egglog-prior':
            assert '--no-decomp' in v.flags, (exp,v.label)
        if v.label == 'no-DVO': assert '--no-dynamic-order' in v.flags
    if exp.startswith('storel') or exp.endswith('-storel'):
        assert all(v.label != 'egglog-prior' for v in e.variants)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('experiments',nargs='*',default=EXPERIMENTS)
    args=ap.parse_args()
    os.chdir(ROOT)
    progress=json.loads(PROGRESS.read_text()) if PROGRESS.exists() else {'created_at':now(),'selection':SELECTION,'experiments':{}}
    for name in args.experiments: audit(name)
    for name in args.experiments:
        prior=progress['experiments'].get(name,{})
        if prior.get('state')=='complete':
            artifacts.verify_run(ROOT/'runs'/prior['run_id']);continue
        entry={'state':'running','started_at':now()};progress['experiments'][name]=entry;save(progress)
        print(f'{now()} START {name}',flush=True)
        try:
            with (ROOT/f'logs/paper/{name}.log').open('w',buffering=1) as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                if query.is_query(name): rd=query.run(name,machine='griffin',tag='paper-final')
                else: rd=runner.run_experiment(config.load_experiment(name),machine='griffin',tag='paper-final')
            entry['run_id']=rd.name
            if query.is_query(name):
                rows=query.load_rows(rd)
                entry['queries']=len(rows)
            else:
                rows=[json.loads(line) for line in (rd/'measurements.jsonl').read_text().splitlines()]
                entry['measurements']=len(rows)
                entry['outcomes']={s:sum(r['status']==s for r in rows) for s in sorted({r['status'] for r in rows})}
            dest=results.select_runs(name,[rd.name],SELECTION)
            entry.update(state='complete',completed_at=now(),selection=str(dest.relative_to(ROOT)))
            with (ROOT/f'logs/paper/{name}-check.log').open('w') as log:
                check=subprocess.run([sys.executable,'-m','harness','check',name,'--selection',SELECTION],stdout=log,stderr=subprocess.STDOUT)
            entry['check_exit_code']=check.returncode
        except (KeyboardInterrupt, SystemExit) as error:
            entry.update(state='interrupted',error=str(error));save(progress);raise
        except Exception as error:
            entry.update(state='failed',error=str(error),traceback=traceback.format_exc())
            print(traceback.format_exc(),flush=True)
        save(progress)
        print(f'{now()} END {name}: {entry["state"]}',flush=True)
    progress['finished_at']=now();save(progress)
if __name__=='__main__': main()
