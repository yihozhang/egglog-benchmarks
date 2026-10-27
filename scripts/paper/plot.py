#!/usr/bin/env python3
"""Generate paper PDFs/PNGs and CSVs exclusively from frozen no-decomposition runs."""
from __future__ import annotations
import argparse, csv, fcntl, hashlib, json, math, statistics, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from harness import config, results, query
from harness.plot import PALETTE, _log_axis
SUITES={'math-microbenchmark':'Math', 'horner':'Horner', 'matmul-assoc':'Matrix multiplication',
        'hardboiled':'Hardboiled','luminal':'Luminal','eggcc':'eggcc','storel':'STOREL'}
WORLD=['hardboiled','luminal','eggcc','storel']
ALG=['math-microbenchmark','horner','matmul-assoc']
KERNELS=['mmm-sum-2nd','mmm-2nd','batax-1st','mttkrp-2nd','ttm-2nd','batax-2nd']
BLUE='#2467a9';ORANGE='#d58020';GRAY='#9a9892'

def save(fig,out,name):
    fig.tight_layout()
    for ext in ['pdf','png']:fig.savefig(out/f'{name}.{ext}',bbox_inches='tight',dpi=200)
    plt.close(fig)

def csvwrite(out,name,rows):
    if not rows:return
    columns=list(dict.fromkeys(k for row in rows for k in row))
    with (out/f'{name}.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(rows)

def load(name,selection):
    if not (ROOT/'results'/name/selection/'selection.json').exists():return None
    selected,_=results.read_selection(name,selection)
    exp=config.experiment_from_dict(name,selected['config'])
    rows=results.load_rows(exp,selection=selection)
    for row in rows:
        if row['system'].startswith('egglog') and row['variant']!='egglog-prior':
            assert '--no-decomp' in row['flags'],row
    return exp,rows,results.summarize(rows)

def val(s):return s['median'] if s and s['status']=='ok' else None

def reached_target(rows):
    return bool(rows) and all(r['status']=='ok'
        and r.get('report',{}).get('stop_reason')=='node-limit'
        and r['report'].get('nodes',0)>=r['report'].get('node_limit',0)
        for r in rows)

def name_of(s,n):
    if s=='math-microbenchmark':return 'Math (12 iterations)' if n.endswith('-12') else 'Math (11 iterations)'
    if s=='matmul-assoc':return 'Matmul-'+n.rsplit('-',1)[1]
    return n.replace('horner','Horner')

def engine_label(v):
    if '-t' in v:return v.rsplit('-t',1)[0]+' ('+v.rsplit('-t',1)[1]+'T)'
    return v

def engines(data,out):
    exp,rows,summ=data
    benches=list(dict.fromkeys((r['suite'],r['benchmark']) for r in rows))
    fixed={v.label:(v.threads or (1,))[0] for v in exp.variants}
    colors={v.label:PALETTE[i%len(PALETTE)] for i,v in enumerate(exp.variants)}
    def draw(bs,name):
        cols=min(4,len(bs));nr=math.ceil(len(bs)/cols)
        fig,axes=plt.subplots(nr,cols,figsize=(5.1*cols,6.2*nr),squeeze=False)
        for ax,(suite,bench) in zip(axes.flat,bs):
            entries=[(v.label,summ.get((suite,bench,v.label,fixed[v.label]))) for v in exp.variants]
            entries.sort(key=lambda e:(val(e[1]) is None,val(e[1]) or 0))
            values=[val(s) for _,s in entries if val(s)]
            floor=10**math.floor(math.log10(min(values)/1.5)) if values else .001
            top=max(values)*2.5 if values else 1
            for y,(v,s) in enumerate(reversed(entries)):
                t=val(s)
                if t:
                    ax.barh(y,t-floor,left=floor,color=colors[v],height=.7)
                    ax.errorbar(t,y,xerr=[[t-s['min']],[s['max']-t]],fmt='none',ecolor='#333333',elinewidth=.65,capsize=1.5)
                    ax.text(t*1.08,y,f'{t:.3g}',va='center',fontsize=7)
                else:
                    status=s['status'] if s else 'missing'
                    label=f'timeout ({exp.timeout:g} s)' if status=='timeout' else status
                    ax.text(floor*1.1,y,label,va='center',fontsize=7,color='#666666')
            ax.set_yticks(range(len(entries)),[engine_label(v) for v,_ in reversed(entries)])
            ax.set_ylim(-.75,len(entries)-.25)
            ax.set_xlim(floor,top);_log_axis(ax,'x');ax.grid(axis='x');ax.grid(False,axis='y')
            ax.set_title(name_of(suite,bench));ax.set_xlabel('Wall time (s), median of 5 runs')
        for ax in list(axes.flat)[len(bs):]:ax.set_visible(False)
        save(fig,out,name)
    draw(benches,'engines')
    for s,n in benches:draw([(s,n)],'engines-'+n)
    csvwrite(out,'engines',[dict(suite=s,benchmark=b,variant=v,threads=t,**{k:x[k] for k in ['status','n','median','min','max']}) for (s,b,v,t),x in summ.items()])

def scaling(data,out):
    exp,rows,summ=data;benches=list(dict.fromkeys((r['suite'],r['benchmark']) for r in rows));records=[]
    fig,axes=plt.subplots(1,len(benches),figsize=(3.7*len(benches),3.2),squeeze=False)
    for ax,(s,n) in zip(axes.flat,benches):
        for v,color in [('egglog-cracked',BLUE),('foresight',ORANGE)]:
            base=val(summ.get((s,n,v,1)));ys=[]
            for t in [1,2,4,8,16]:
                x=summ.get((s,n,v,t));time=val(x);ratio=base/time if base and time else np.nan;ys.append(ratio)
                records.append(dict(suite=s,benchmark=n,variant=v,threads=t,status=x['status'] if x else 'missing',median_s=time,speedup=ratio))
            ax.plot([1,2,4,8,16],ys,'o-',color=color,label=v,linewidth=1.6,markersize=4)
        ax.plot([1,2,4,8,16],[1,2,4,8,16],'--',color=GRAY,label='Ideal linear scaling')
        ax.set_xscale('log',base=2);ax.set_yscale('log',base=2)
        ax.set_xticks([1,2,4,8,16],[1,2,4,8,16]);ax.set_yticks([.5,1,2,4,8,16],['0.5','1','2','4','8','16'])
        ax.set_ylim(.45,20);ax.set_xlabel('Threads');ax.set_title(name_of(s,n));ax.grid(axis='both')
    axes[0,0].set_ylabel('Speedup over each engine’s 1-thread run')
    axes[0,-1].legend(frameon=False,fontsize=7)
    save(fig,out,'scaling-algebraic');csvwrite(out,'scaling-algebraic',records)

def compare(datasets,baseline_for,threads_pair=(1,1),variant='egglog-cracked'):
    records=[]
    for exp,rows,summ in datasets:
        for s,n in dict.fromkeys((r['suite'],r['benchmark']) for r in rows):
            baseline=baseline_for(s);a=summ.get((s,n,baseline,threads_pair[0]));b=summ.get((s,n,variant,threads_pair[1]));av,bv=val(a),val(b)
            ast=a['status'] if a else 'missing';bst=b['status'] if b else 'missing'
            if s=='storel':
                for label,thread,side in [(baseline,threads_pair[0],'a'),(variant,threads_pair[1],'b')]:
                    samples=[r for r in rows if r['benchmark']==n and r['variant']==label and r['threads']==thread]
                    if not reached_target(samples):
                        if side=='a' and ast=='ok':ast='below-target'
                        if side=='b' and bst=='ok':bst='below-target'
            left=(a or {}).get('sizes') or {};right=(b or {}).get('sizes') or {}
            differing=[k for k in left.keys()&right.keys() if left[k]!=right[k]]
            records.append(dict(suite=s,benchmark=n,baseline=baseline,baseline_threads=threads_pair[0],threads=threads_pair[1],baseline_status=ast,status=bst,baseline_completed_repetitions=a['n'] if a else 0,completed_repetitions=b['n'] if b else 0,baseline_median_s=av,median_s=bv,speedup=av/bv if av and bv and ast==bst=='ok' else None,speedup_lower_bound=exp.timeout/bv if ast=='timeout' and a['n']==0 and bst=='ok' and bv else None,timeout_s=exp.timeout,table_size_mismatches=len(differing),differing_tables=','.join(sorted(differing))))
    return records

def comparison_plot(records,out,name,suites,ylabel,ideal=None,subtitles=None):
    fig,ax=plt.subplots(figsize=(max(8.2,1.65*len(suites)),3.8));ticks=[];stats=[]
    bounds_labeled=False
    for x,suite in enumerate(suites):
        rs=[r for r in records if r['suite']==suite];good=[r for r in rs if r['speedup'] is not None]
        values=[r['speedup'] for r in good];g=results.geomean(values)
        jitter=np.linspace(-.24,.24,len(good))
        ax.scatter(x+jitter,values,s=17,color=BLUE,alpha=.55,label='Individual benchmark' if x==0 else None,zorder=3)
        bounds=[r['speedup_lower_bound'] for r in rs if r['speedup_lower_bound'] is not None]
        if bounds:
            ax.scatter(x+np.linspace(-.24,.24,len(bounds)),bounds,marker='^',s=22,facecolors='none',edgecolors=BLUE,label='Baseline timeout: lower bound' if not bounds_labeled else None,zorder=3)
            bounds_labeled=True
        if g:
            ax.plot([x-.3,x+.3],[g,g],color='#112d4e',lw=3,zorder=4,label='Geometric mean' if x==0 else None)
            ax.annotate(f'{g:.2f}×',(x,g),xytext=(0,6),textcoords='offset points',ha='center',fontsize=8,bbox=dict(facecolor='white',edgecolor='none',pad=.6,alpha=.85),zorder=5)
        sub=subtitles(suite) if subtitles else ''
        ticks.append(SUITES[suite]+('\n'+sub if sub else '')+f'\nn = {len(good)}/{len(rs)}')
        stats.append(dict(suite=suite,geomean_speedup=g,completed_pairs=len(good),total_pairs=len(rs),excluded_pairs=len(rs)-len(good),pairs_with_table_size_differences=sum(bool(r['table_size_mismatches']) for r in good)))
    ax.axhline(1,color=GRAY,lw=1,ls='--',label='Baseline (1.0×)')
    if ideal:ax.axhline(ideal,color=ORANGE,lw=1,ls=':',label=f'Ideal ({ideal}×)')
    ax.set_yscale('log');ax.set_ylabel(ylabel);ax.set_xticks(range(len(suites)),ticks)
    ax.legend(frameon=False,fontsize=7,loc='upper left',bbox_to_anchor=(1.01,1));ax.set_xlim(-.6,len(suites)-.4)
    notes=[]
    changed=sorted({r['suite'] for r in records if r['table_size_mismatches'] and r['suite']!='storel'})
    if changed:notes.append(', '.join(SUITES[s] for s in changed)+': original schedule runtimes; final table sizes differ between configurations.')
    if 'storel' in suites:notes.append('STOREL: node-limited runs; compare actual reached sizes in the accompanying work data.')
    if bounds_labeled:notes.append('Triangles use timeout budget / cracked runtime; geometric means include only completed pairs.')
    if notes:fig.text(.5,-.02,'\n'.join(notes),ha='center',va='top',fontsize=7)
    save(fig,out,name);csvwrite(out,name,records);csvwrite(out,name+'-summary',stats)
    # Per-benchmark bars retain unsuccessful outcomes explicitly.
    fig,axes=plt.subplots(len(suites),1,figsize=(14,2.3*len(suites)),squeeze=False)
    fig.text(.5,1.005,ylabel,ha='center',va='bottom',fontsize=11)
    for ax,suite in zip(axes.flat,suites):
        rs=[r for r in records if r['suite']==suite];ys=[r['speedup'] if r['speedup'] is not None else np.nan for r in rs]
        ax.bar(range(len(rs)),np.array(ys)-1,bottom=1,color=BLUE,width=.72);ax.axhline(1,color=GRAY,ls='--',lw=.8)
        ax.set_yscale('log');ax.set_ylabel('Speedup');ax.set_title(SUITES[suite])
        ax.set_xticks(range(len(rs)),[r['benchmark'] for r in rs],rotation=45,ha='right',fontsize=6)
        extent=[1.0]+[y for y in ys if np.isfinite(y)]+[r['speedup_lower_bound'] for r in rs if r['speedup_lower_bound'] is not None]
        lower,upper=min(.5,min(extent)*.8),max(2.,max(extent)*1.35)
        ax.set_ylim(lower,upper)
        if (lower,upper)==(.5,2.):
            ax.set_yticks([.5,1,2],['0.5','1','2'])
            ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
            if len(rs)<=7:
                for i,y in enumerate(ys):
                    if np.isfinite(y):
                        ax.annotate(f'{y:.3f}×',(i,y),xytext=(0,5 if y>=1 else -5),textcoords='offset points',ha='center',va='bottom' if y>=1 else 'top',fontsize=6)
        for i,r in enumerate(rs):
            if r['speedup_lower_bound'] is not None:
                bound=r['speedup_lower_bound'];ax.scatter([i],[bound],marker='^',facecolors='none',edgecolors=BLUE,s=25)
            elif r['speedup'] is None:ax.text(i,1,r['baseline_status']+'/'+r['status'],rotation=90,va='bottom',ha='center',fontsize=6,color='#a13b2d')
    if notes:fig.text(.5,-.01,'\n'.join(notes),ha='center',va='top',fontsize=7)
    save(fig,out,name+'-details')

def storel(data,out):
    exp,rows,summ=data;records=[];fig,axes=plt.subplots(1,2,figsize=(10,3.5))
    for i,v in enumerate(['egg','egglog-cracked']):
        xs=np.arange(len(KERNELS))+(i-.5)*.36;times=[];nodes=[]
        for k in KERNELS:
            rs=[r for r in rows if r['benchmark']==k and r['variant']==v];success=rs and all(r['status']=='ok' for r in rs)
            t=statistics.median(r['wall_s'] for r in rs) if success else np.nan
            n=statistics.median(r['report']['nodes'] for r in rs) if success else np.nan
            times.append(t);nodes.append(n)
            records.append(dict(benchmark=k,variant=v,median_s=t,median_nodes=n,node_limit=next((r.get('report',{}).get('node_limit') for r in rows if r['benchmark']==k and r.get('report',{}).get('node_limit')),None),status='ok' if success else '/'.join(sorted({r['status'] for r in rs})),stop_reasons=','.join(sorted({r.get('report',{}).get('stop_reason','unknown') for r in rs}))))
        for ax,values in zip(axes,[times,nodes]):ax.bar(xs,values,.33,label=v,color=[ORANGE,BLUE][i])
    for x,k in enumerate(KERNELS):
        limit=next((r.get('report',{}).get('node_limit') for r in rows if r['benchmark']==k and r.get('report',{}).get('node_limit')),None)
        if limit:axes[1].hlines(limit,x-.43,x+.43,color='black',ls='--',lw=.8)
    for ax in axes:ax.set_xticks(range(len(KERNELS)),KERNELS,rotation=30,ha='right');ax.set_yscale('log');ax.legend(frameon=False)
    axes[0].set_ylabel('Wall time to stopping condition (s)');axes[0].set_title('STOREL: backoff scheduler')
    axes[1].set_ylabel('E-nodes at stop');axes[1].set_title('Actual work reached; target dashed')
    save(fig,out,'storel-backoff');csvwrite(out,'storel-backoff',records)

def schedules(data,out):
    exp,rows,summ=data;records=[];ks=[1,2,4,8];fig,axes=plt.subplots(1,3,figsize=(12,3.4))
    workfig,workaxes=plt.subplots(1,2,figsize=(8,3.4),sharey=True)
    excluded=[]
    for ci,kernel in enumerate(KERNELS):
        metrics=[[],[],[]];node_metrics=[[],[]];node_ranges=[[],[]];target_flags=[[],[]]
        for k in ks:
            v=f'egglog-cracked-batch-{k}';vals={t:val(summ.get(('storel',kernel,v,t))) for t in [1,8]}
            base={t:val(summ.get(('storel',kernel,'egglog-cracked-batch-1',t))) for t in [1,8]}
            samples={t:[r for r in rows if r['benchmark']==kernel and r['variant']==v and r['threads']==t] for t in [1,8]}
            target={t:reached_target(samples[t]) for t in [1,8]}
            baseline_target={t:reached_target([r for r in rows if r['benchmark']==kernel and r['variant']=='egglog-cracked-batch-1' and r['threads']==t]) for t in [1,8]}
            metrics[0].append(vals[1]/vals[8] if vals[1] and vals[8] and all(target.values()) else np.nan)
            for j,t in enumerate([1,8]):
                metrics[j+1].append(base[t]/vals[t] if base[t] and vals[t] and target[t] and baseline_target[t] else np.nan)
                rs=samples[t]
                ok=rs and all(r['status']=='ok' for r in rs)
                nodes=statistics.median(r['report']['nodes'] for r in rs) if ok else None
                limit=next((r.get('report',{}).get('node_limit') for r in rs if r.get('report',{}).get('node_limit')),None)
                node_metrics[j].append(nodes/limit if nodes is not None and limit else np.nan)
                node_min=min(r['report']['nodes'] for r in rs) if ok else None
                node_max=max(r['report']['nodes'] for r in rs) if ok else None
                node_ranges[j].append(((nodes-node_min)/limit,(node_max-nodes)/limit) if ok and limit else (0,0))
                target_flags[j].append(target[t])
                stops=','.join(sorted({r.get('report',{}).get('stop_reason','unknown') for r in rs}))
                if not target[t]:excluded.append(f'{kernel}, K={k}, {t}T ({stops})')
                records.append(dict(benchmark=kernel,rewrite_batch=k,threads=t,median_s=vals[t],median_nodes=nodes,min_nodes=node_min,max_nodes=node_max,node_limit=limit,median_iterations=statistics.median(r['report']['iterations'] for r in rs) if ok else None,stop_reasons=stops,target_reached=target[t],repetitions_reaching_target=sum(reached_target([r]) for r in rs),repetitions=len(rs),speedup_over_same_batch_1t=metrics[0][-1] if t==8 else (1 if target[1] else None),speedup_over_original_schedule=metrics[j+1][-1],status='ok' if ok else '/'.join(sorted({r['status'] for r in rs}))))
        for ax,ys in zip(axes,metrics):ax.plot(ks,ys,'o-',color=PALETTE[ci],label=kernel,markersize=3,lw=1)
        for j,(ax,ys) in enumerate(zip(workaxes,node_metrics)):
            ax.errorbar(ks,ys,yerr=np.array(node_ranges[j]).T,fmt='o-',color=PALETTE[ci],label=kernel,markersize=3,lw=1,capsize=2)
            missed=[i for i,good in enumerate(target_flags[j]) if not good]
            if missed:ax.scatter([ks[i] for i in missed],[ys[i] for i in missed],marker='x',s=42,color='black',zorder=5)
    for ax,title in zip(axes,['8-thread speedup within each schedule','1 thread: speedup over original schedule','8 threads: speedup over original schedule']):
        ax.set_title(title);ax.set_ylabel('Speedup');ax.axhline(1,color=GRAY,ls='--');ax.set_yscale('log',base=2)
    axes[0].axhline(8,color=GRAY,ls=':',label='Ideal 8-thread scaling')
    axes[2].legend(frameon=False,fontsize=6)
    for ax,t in zip(workaxes,[1,8]):ax.set_title(f'{t} thread(s)');ax.set_ylabel('E-nodes at stop / target');ax.set_yscale('log');ax.axhline(1,color=GRAY,ls='--')
    workaxes[1].legend(frameon=False,fontsize=6)
    for ax in [*axes,*workaxes]:ax.set_xscale('log',base=2);ax.set_xticks(ks,ks);ax.set_xlabel('Executions of each rewrite phase per aux refresh')
    if excluded:
        fig.text(.5,-.035,f'Speedups require every repetition to reach its node target; {len(excluded)}/48 configurations are excluded. See work figure and CSV.',ha='center',fontsize=7)
        workfig.text(.5,-.035,'Median with min–max range. Black ×: at least one repetition stopped below target.',ha='center',fontsize=7)
    save(fig,out,'storel-schedule');save(workfig,out,'storel-schedule-work');csvwrite(out,'storel-schedule',records)

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--selection',default='paper-cracked-20261025');ap.add_argument('--out',type=Path,default=ROOT/'figures/paper');args=ap.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    plot_lock=(args.out/'.plot.lock').open('a')
    fcntl.flock(plot_lock,fcntl.LOCK_EX)
    names=['engines','scaling','e2e','storel','scaling-real-world','scaling-storel','ablation','ablation-storel','storel-schedule'];ds={name:load(name,args.selection) for name in names}
    if ds['engines']:engines(ds['engines'],args.out)
    if ds['scaling']:scaling(ds['scaling'],args.out)
    if ds['storel']:storel(ds['storel'],args.out)
    if ds['e2e'] and ds['storel']:
        rs=compare([ds['e2e'],ds['storel']],lambda s:'egg' if s=='storel' else 'egglog-prior')
        comparison_plot(rs,args.out,'real-world',WORLD,'egglog-cracked speedup over baseline',subtitles=lambda s:'vs egg (backoff)' if s=='storel' else 'vs egglog-prior')
    if ds['scaling-real-world'] and ds['scaling-storel']:
        rs=compare([ds['scaling-real-world'],ds['scaling-storel']],lambda s:'egglog-cracked',(1,8))
        comparison_plot(rs,args.out,'scaling-real-world',WORLD,'egglog-cracked: 8-thread speedup over 1 thread',ideal=8)
    if ds['ablation'] and ds['ablation-storel']:
        for baseline,name,title in [('no-DVO','ablation','no-DVO (size order)'),('no-DVO-refinement','ablation-refinement','no-DVO (refinement order)')]:
            rs=compare([ds['ablation'],ds['ablation-storel']],lambda s:baseline)
            comparison_plot(rs,args.out,name,ALG+WORLD,f'egglog-cracked speedup over {title}')
    if ds['storel-schedule']:schedules(ds['storel-schedule'],args.out)
    available=[n for n,d in ds.items() if d]
    if (ROOT/'results/query'/args.selection/'selection.json').exists():
        query.plot('query',selection=args.selection,out_dir=args.out);available.append('query')
    sources={n:hashlib.sha256((ROOT/'results'/n/args.selection/'selection.json').read_bytes()).hexdigest() for n in available}
    (args.out/'sources.json').write_text(json.dumps({'selection':args.selection,'available_experiments':available,'selection_sha256':sources,'metric':'wall_s except database query comparison; see README','no_decomp':True},indent=2)+'\n')
    manifest={'selection':args.selection,
              'plot_script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'render_dependencies_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in ['harness/plot.py','harness/query.py']},
              'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted(args.out.iterdir()) if p.suffix in ('.pdf','.png','.csv')}}
    (args.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (ROOT/'results/paper/figures.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print('Figures:',args.out)
if __name__=='__main__':main()
