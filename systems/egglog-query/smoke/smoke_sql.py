"""Smoke test: run the translated gemma queries on DuckDB or Umbra.
Load (untimed) -> per query: EXPLAIN (timed = planning), CREATE TEMP TABLE AS SELECT DISTINCT
(timed = total), count rows, DROP. Median of REPS. Times measured here with perf_counter."""
import csv, json, statistics, sys, time, pathlib
sys.path.insert(0, "/home/yz489/egglog-benchmarks/systems/egglog-query")
import sqlgen

Q = pathlib.Path("/home/yz489/egglog-benchmarks/build/query")
engine, threads = sys.argv[1], int(sys.argv[2])
subsumption = "--subsumption" in sys.argv
REPS = 5
schema = sqlgen.Schema(Q / "gemma/schema.json")
queries = {f"q{k}": (Q / f"smoke/q{k}.sql").read_text() for k in range(1, 5)}
tables = sorted({t for k in range(1, 5) for t in json.loads((Q / f"smoke/q{k}.tables.json").read_text())})
if subsumption:
    queries = {}
    for k in range(1, 5):
        pass  # regenerated below

def rows_for(t):
    out = []
    has_out = schema.has_out(t)
    with open(Q / f"gemma/tables/{t}.csv", newline="") as f:
        for r in csv.reader(f):
            sub = r[-1] == "true"; vals = r[:-1]
            if not has_out:
                vals = vals[:schema.ncols(t)]
            if subsumption:
                out.append(vals + [not sub])
            elif not sub:
                out.append(vals)
    return out

if engine == "duckdb":
    import duckdb
    con = duckdb.connect()
    con.execute(f"SET threads = {threads}")
    ex = lambda s: con.execute(s)
    fetch = lambda s: con.execute(s).fetchall()
else:
    import psycopg
    con = psycopg.connect("host=localhost port=5432 user=postgres password=postgres dbname=postgres", autocommit=True)
    cur = con.cursor()
    ex = lambda s: cur.execute(s)
    def fetch(s):
        cur.execute(s); return cur.fetchall()

for t in tables:
    print("load", t, file=sys.stderr, flush=True)
    if engine == 'duckdb': ex(f'DROP TABLE IF EXISTS "{t}"')
    ex(schema.create_table(t, subsumption))
    rs = rows_for(t)
    if rs:
        ph = ", ".join(["%s" if engine != "duckdb" else "?"] * len(rs[0]))
        if engine == "duckdb":
            con.executemany(f'INSERT INTO "{t}" VALUES ({ph})', rs)
        else:
            cur.executemany(f'INSERT INTO "{t}" VALUES ({ph})', rs)
    if engine == "duckdb":
        ex(f'ANALYZE "{t}"')
    else:
        ex(f'ANALYZE "{t}"')

for name, sql in queries.items():
    sel = sql[sql.index("SELECT"):].rstrip(";")
    plan, total, n = [], [], None
    for rep in range(REPS + 1):  # first rep is a warm-up
        # a fresh result table per repetition (no DROP: DROP crashes the Umbra server here)
        stmt = sql.replace(f"r_{name} AS", f"r_{name}_{rep} AS", 1)
        t0 = time.perf_counter(); fetch("EXPLAIN " + sel); t1 = time.perf_counter()
        ex(stmt); t2 = time.perf_counter()
        n = fetch(f"SELECT count(*) FROM r_{name}_{rep}")[0][0]
        plan.append(t1 - t0); total.append(t2 - t1)
    p, tt = statistics.median(plan[1:]), statistics.median(total[1:])
    print(json.dumps({"engine": engine, "threads": threads, "query": name, "rows": n,
                      "total_ms": tt * 1e3, "plan_ms": p * 1e3, "exec_ms": (tt - p) * 1e3}))
