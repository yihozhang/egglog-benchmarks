import sys, json, csv, pathlib, psycopg, subprocess, time
sys.path.insert(0, "systems/egglog-query"); import sqlgen
Q=pathlib.Path("/home/yz489/egglog-benchmarks/build/query"); schema=sqlgen.Schema(Q/"gemma/schema.json")
rule=sqlgen.parse_sexp((Q/"smoke/q3.rule.egg").read_text())
lhs=rule[1]
def emit(x): return "("+" ".join(emit(y) for y in x)+")" if isinstance(x,list) else x
def run(k):
    subprocess.run("podman rm -f umbra-smoke >/dev/null 2>&1; podman run -d --name umbra-smoke -p 5432:5432 docker.io/umbradb/umbra:latest >/dev/null; sleep 8", shell=True)
    text=f"(rule ({' '.join(emit(f) for f in lhs[:k])}) ((union ?matmul ?matmul)))"
    r=sqlgen.translate(schema, text, "b")
    c=psycopg.connect("host=localhost port=5432 user=postgres password=postgres dbname=postgres", autocommit=True).cursor()
    for t in r["tables"]:
        c.execute(schema.create_table(t, False))
        rows=[x[:-1] if schema.has_out(t) else x[:schema.ncols(t)] for x in csv.reader(open(Q/f"gemma/tables/{t}.csv")) if x[-1]=="false"]
        if rows: c.executemany(f'INSERT INTO "{t}" VALUES ({", ".join(["%s"]*len(rows[0]))})', rows)
    sel=r["sql"][r["sql"].index("SELECT"):].rstrip(";")
    try:
        n=c.execute(f"SELECT count(*) FROM ({sel}) x").fetchall()[0][0]; print(f"k={k:2d} atoms={r['atoms']:2d} ok rows={n}", flush=True); return True
    except Exception as e:
        print(f"k={k:2d} atoms={r['atoms']:2d} CRASH", flush=True); return False
lo, hi = 1, len(lhs)   # lo passes (assumed), hi crashes
assert not run(hi)
while hi - lo > 1:
    mid=(lo+hi)//2
    if run(mid): lo=mid
    else: hi=mid
print("smallest crashing prefix:", hi, "facts; last fact:", emit(lhs[hi-1])[:200])
