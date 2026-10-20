#!/usr/bin/env python3
"""Database side of the query-only benchmark for one exported program.

Usage: bench_db.py <duckdb|umbra> <program-dir> [--threads N] [--reps N] [--timeout S] [--only q1,q2]
<program-dir> is what run_suite.py wrote (schema.json, tables/, queries/, index.json).
Writes <program-dir>/<engine>.jsonl, one line per query.

All tables any query reads are created (functional dependency as PRIMARY KEY, subsumed rows
dropped) and loaded once per process, untimed. Each query's result table
`res_<q>` is created empty beforehand (untimed) and cleared with DELETE before every run.
Times are medians over --reps runs after one warm-up run.

DuckDB (embedded): ad-hoc `INSERT INTO res_<q> SELECT DISTINCT ...`, profiled. Recorded:
  wall_ms (ours), latency_ms, and from DuckDB's profiler planner_ms (binding), optimizer_ms
  (sum of all optimizers), physical_planner_ms, operator_ms (cpu_time);
  optimize_ms = planner + optimizer + physical planner, execute_ms = latency - optimize_ms.
Umbra (server in a container, one per program; restarted and reloaded after a crash):
  prepare_ms = wall of `PREPARE p AS INSERT INTO res_<q> SELECT DISTINCT ...` (parse, plan,
  optimize, generate code), execute_ms = wall of `EXECUTE p` (verified not to re-plan: Umbra
  logs ~0.03 ms compilation for EXECUTE), adhoc_ms = wall of the same INSERT ad hoc (reference).
"""
import argparse, csv, json, os, pathlib, shutil, statistics, subprocess, sys, threading, time
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); import sqlgen
UMBRA_IMAGE = "docker.io/umbradb/umbra:latest"

def container_engine():
    """Match the container adapters, including an explicit executable override."""
    return os.environ.get("CONTAINER_ENGINE") or ("podman" if shutil.which("podman") else "docker")


def remove_umbra(engine):
    subprocess.run([engine, "rm", "-f", "umbra-bench"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def rows_for(t):
    has_out, n = schema.has_out(t), schema.ncols(t)
    with open(D / f"tables/{t}.csv", newline="") as f:
        return [(r[:-1] if has_out else r[:n]) for r in csv.reader(f) if r[-1] == "false"]

def select_of(sql):
    return sql[sql.index("SELECT"):].rstrip().rstrip(";")

class Timeout:
    """Cancel the running statement after `s` seconds (cancel_fn is called from a timer thread)."""
    def __init__(self, s, cancel_fn): self.s, self.cancel_fn, self.fired = s, cancel_fn, False
    def __enter__(self):
        def fire(): self.fired = True; self.cancel_fn()
        self.t = threading.Timer(self.s, fire); self.t.start(); return self
    def __exit__(self, *exc): self.t.cancel()

# ---------------------------------------------------------------- DuckDB
def run_duckdb(out):
    import duckdb
    con = duckdb.connect(); con.execute(f"SET threads = {a.threads}")
    for t in tables:
        con.execute(schema.create_table(t, False)); rs = rows_for(t)
        if rs: con.executemany(f'INSERT INTO "{t}" VALUES ({",".join("?" * len(rs[0]))})', rs)
        con.execute(f'ANALYZE "{t}"')
    con.execute("SET enable_profiling = 'json'")
    con.execute("""SET custom_profiling_settings = '{"LATENCY":"true","PLANNER":"true","PLANNER_BINDING":"true","ALL_OPTIMIZERS":"true","PHYSICAL_PLANNER":"true","CPU_TIME":"true","OPERATOR_TIMING":"true"}'""")
    prof = SCRATCH / f"duckdb-profile-{os.getpid()}.json"
    con.execute(f"SET profiling_output = '{prof}'")
    for qid, sql, _ in queries:
        sel = select_of(sql); rec = {"engine": "duckdb", "threads": a.threads, "query": qid}
        try:
            con.execute(f"CREATE TEMP TABLE res_{qid} AS {sel} LIMIT 0")
            runs = []
            for rep in range(a.reps + 1):
                con.execute(f"DELETE FROM res_{qid}")
                if prof.exists(): prof.unlink()
                with Timeout(a.timeout, con.interrupt) as to:
                    t0 = time.perf_counter(); con.execute(f"INSERT INTO res_{qid} {sel}"); w = time.perf_counter() - t0
                p = json.load(open(prof))
                opt = sum(v for k, v in p.items() if k.startswith("optimizer_") and isinstance(v, (int, float)))
                runs.append({"wall_ms": w * 1e3, "latency_ms": p["latency"] * 1e3, "planner_ms": p.get("planner", 0) * 1e3,
                             "optimizer_ms": opt * 1e3, "physical_planner_ms": p.get("physical_planner", 0) * 1e3,
                             "operator_ms": p.get("cpu_time", 0) * 1e3})
            m = {k: statistics.median(r[k] for r in runs[1:]) for k in runs[0]}
            m["optimize_ms"] = m["planner_ms"] + m["optimizer_ms"] + m["physical_planner_ms"]
            m["execute_ms"] = m["latency_ms"] - m["optimize_ms"]
            rec.update(status="ok", rows=con.execute(f"SELECT count(*) FROM res_{qid}").fetchone()[0], **m)
        except Exception as e:
            rec.update(status="timeout" if "INTERRUPT" in str(e).upper() else "error", error=str(e)[:300])
        out.write(json.dumps(rec) + "\n"); out.flush()

# ---------------------------------------------------------------- Umbra
def umbra_connect():
    import psycopg
    engine = container_engine()
    con = None
    try:
        for attempt in range(5):   # a crashed server's container sometimes refuses to stop at first
            remove_umbra(engine)
            # Explicit removal works with both engines; Docker has no --replace option.
            if subprocess.run([engine, "run", "-d", "--name", "umbra-bench", "-p", "5432:5432", UMBRA_IMAGE],
                              stdout=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(5)
        else:
            raise RuntimeError("could not start the Umbra container")
        last_error = None
        for _ in range(240):
            try:
                con = psycopg.connect("host=localhost port=5432 user=postgres password=postgres dbname=postgres",
                                      autocommit=True, connect_timeout=2)
                break
            except Exception as error:
                last_error = error
                time.sleep(0.5)
        else:
            raise RuntimeError("Umbra did not accept a connection after 240 attempts") from last_error
        c = con.cursor()
        for t in tables:
            c.execute(schema.create_table(t, False)); rs = rows_for(t)
            if rs: c.executemany(f'INSERT INTO "{t}" VALUES ({",".join(["%s"] * len(rs[0]))})', rs)
            c.execute(f'ANALYZE "{t}"')
        return con, c
    except BaseException:
        if con is not None:
            con.close()
        remove_umbra(engine)
        raise

def run_umbra(out):
    con = None
    try:
        con, c = umbra_connect()
        for qid, sql, _ in queries:
            sel = select_of(sql); rec = {"engine": "umbra", "threads": a.threads, "query": qid}
            timed = lambda s: (lambda t0: (c.execute(s), time.perf_counter() - t0)[1])(time.perf_counter())
            try:
                with Timeout(a.timeout, con.cancel) as to:
                    c.execute(f"CREATE TEMP TABLE res_{qid} AS {sel} LIMIT 0")
                    prep = timed(f"PREPARE p_{qid} AS INSERT INTO res_{qid} {sel}")
                exe, adhoc = [], []
                for rep in range(a.reps + 1):
                    c.execute(f"DELETE FROM res_{qid}")
                    with Timeout(a.timeout, con.cancel): exe.append(timed(f"EXECUTE p_{qid}"))
                rows = c.execute(f"SELECT count(*) FROM res_{qid}").fetchone()[0]
                for rep in range(a.reps + 1):
                    c.execute(f"DELETE FROM res_{qid}")
                    with Timeout(a.timeout, con.cancel): adhoc.append(timed(f"INSERT INTO res_{qid} {sel}"))
                med = lambda xs: statistics.median(xs[1:]) * 1e3
                rec.update(status="ok", rows=rows, prepare_ms=prep * 1e3, execute_ms=med(exe), adhoc_ms=med(adhoc))
            except Exception as e:
                alive = subprocess.run([container_engine(), "ps", "-q", "--filter", "name=umbra-bench"],
                                       capture_output=True, text=True).stdout.strip()
                rec.update(status="timeout" if "cancel" in str(e).lower() and alive else ("crash" if not alive else "error"), error=str(e)[:300])
                if not alive or rec["status"] != "ok":
                    try: con.close()
                    except Exception: pass
                    con, c = umbra_connect()
            out.write(json.dumps(rec) + "\n"); out.flush()
    finally:
        if con is not None:
            try: con.close()
            except Exception: pass
        remove_umbra(container_engine())


def main():
    global a, D, schema, index, queries, tables, SCRATCH
    ap = argparse.ArgumentParser()
    ap.add_argument("engine"); ap.add_argument("dir", type=pathlib.Path)
    ap.add_argument("--threads", type=int, default=1); ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=60); ap.add_argument("--only")
    a = ap.parse_args()
    D = a.dir.resolve(); schema = sqlgen.Schema(D / "schema.json")
    index = json.load(open(D / "index.json"))["queries"]
    if a.only: index = [q for q in index if q["id"] in a.only.split(",")]
    queries = [(q["id"], (D / "queries" / f"{q['id']}.sql").read_text(), json.loads((D / "queries" / f"{q['id']}.tables.json").read_text())) for q in index]
    tables = sorted({t for _, _, ts in queries for t in ts})
    SCRATCH = pathlib.Path(os.environ.get("BENCH_SCRATCH", "/tmp")); SCRATCH.mkdir(parents=True, exist_ok=True)
    with open(D / f"{a.engine}.jsonl", "w") as out:
        {"duckdb": run_duckdb, "umbra": run_umbra}[a.engine](out)


if __name__ == "__main__":
    main()
