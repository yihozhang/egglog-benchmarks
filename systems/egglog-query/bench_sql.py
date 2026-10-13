#!/usr/bin/env python3
"""Run translated queries on DuckDB or Umbra and print one JSON line per query.

Usage: bench_sql.py <duckdb|umbra> <threads> <export-dir> <query-dir> <id>... [--subsumption] [--reps N]

For each query: create and load only the tables it reads (untimed; subsumed rows are dropped
unless --subsumption), ANALYZE, then REPS+1 repetitions (the first is a warm-up) of
  EXPLAIN <select>                                  timed: planning
  CREATE TEMP TABLE r_<id>_<rep> AS SELECT DISTINCT  timed: total
and report the medians and the result row count. Every Umbra query runs in a fresh server
container, so a crash (reported as "crash") only affects that query. Times: perf_counter here.
"""
import csv, json, pathlib, statistics, subprocess, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).parent)); import sqlgen

args = [a for a in sys.argv[1:] if not a.startswith("--")]
engine, threads, export_dir, qdir, ids = args[0], int(args[1]), pathlib.Path(args[2]), pathlib.Path(args[3]), args[4:]
subsumption = "--subsumption" in sys.argv
REPS = int(sys.argv[sys.argv.index("--reps") + 1]) if "--reps" in sys.argv else 5
if "--reps" in sys.argv: ids = [i for i in ids if i != str(REPS)]
schema = sqlgen.Schema(export_dir / "schema.json")
UMBRA_IMAGE = "docker.io/umbradb/umbra:latest"

def rows_for(t):
    out, has_out = [], schema.has_out(t)
    with open(export_dir / f"tables/{t}.csv", newline="") as f:
        for r in csv.reader(f):
            sub, vals = r[-1] == "true", r[:-1]
            if not has_out:
                vals = vals[:schema.ncols(t)]
            if subsumption:
                out.append(vals + [not sub])
            elif not sub:
                out.append(vals)
    return out

def connect():
    if engine == "duckdb":
        import duckdb
        con = duckdb.connect(); con.execute(f"SET threads = {threads}")
        return con, (lambda s: con.execute(s)), (lambda s: con.execute(s).fetchall()), (lambda s, rs: con.executemany(s, rs)), "?"
    import psycopg
    subprocess.run(f"podman run -d --replace --name umbra-bench -p 5432:5432 {UMBRA_IMAGE} >/dev/null", shell=True, check=True)
    for _ in range(60):
        try:
            con = psycopg.connect("host=localhost port=5432 user=postgres password=postgres dbname=postgres", autocommit=True); break
        except Exception:
            time.sleep(0.5)
    cur = con.cursor()
    def fetch(s):
        cur.execute(s); return cur.fetchall()
    return con, (lambda s: cur.execute(s)), fetch, (lambda s, rs: cur.executemany(s, rs)), "%s"

con = None
for qid in ids:
    sql = (qdir / f"{qid}.sql").read_text()
    tables = json.loads((qdir / f"{qid}.tables.json").read_text())
    if subsumption:
        raise SystemExit("regenerate queries with subsumption=True first")
    if engine == "umbra" or con is None:
        con, ex, fetch, many, ph = connect()
    rec = {"engine": engine, "threads": threads, "query": qid}
    try:
        for t in tables:
            if engine == "duckdb":
                ex(f'DROP TABLE IF EXISTS "{t}"')
            ex(schema.create_table(t, subsumption))
            rs = rows_for(t)
            if rs:
                many(f'INSERT INTO "{t}" VALUES ({", ".join([ph] * len(rs[0]))})', rs)
            ex(f'ANALYZE "{t}"')
        sel = sql[sql.index("SELECT"):].rstrip().rstrip(";")
        plan, total = [], []
        for rep in range(REPS + 1):
            stmt = sql.replace(f"r_{qid} AS", f"r_{qid}_{rep} AS", 1)
            t0 = time.perf_counter(); fetch("EXPLAIN " + sel); t1 = time.perf_counter()
            ex(stmt); t2 = time.perf_counter()
            rec["rows"] = fetch(f"SELECT count(*) FROM r_{qid}_{rep}")[0][0]
            plan.append(t1 - t0); total.append(t2 - t1)
        rec.update(status="ok", total_ms=statistics.median(total[1:]) * 1e3, plan_ms=statistics.median(plan[1:]) * 1e3)
    except Exception as e:
        rec.update(status="crash" if engine == "umbra" else "error", error=str(e)[:200])
        con = None
    print(json.dumps(rec), flush=True)
if engine == "umbra":
    subprocess.run("podman rm -f umbra-bench >/dev/null 2>&1", shell=True)
