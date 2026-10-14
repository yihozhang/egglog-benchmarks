"""Parallel delta debugging of the Umbra crash.

Order: (1) join conditions on the full data, (2) FROM aliases (an alias goes with every
condition that mentions it), (3) rows of each remaining table. Each test runs the SQL in a
fresh container with `umbra-sql -createdb` under a 60 s timeout; it "crashes" when umbra-sql
dies from a signal (exit status >= 128, other than the timeout's 124). ddmin evaluates all
candidate subsets/complements of a round in parallel and keeps the first one (in order) that
still crashes. Progress goes to reduce2.log; the result to repro-min.sql.
"""
import re, subprocess, pathlib, sys, time
from concurrent.futures import ThreadPoolExecutor

HERE = pathlib.Path(__file__).parent
IMAGE = "docker.io/umbradb/umbra:latest"
POOL = ThreadPoolExecutor(max_workers=32)
LOG = open(HERE / "reduce2.log", "a")
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=LOG, flush=True)

lines = (HERE / "repro-full.sql").read_text().splitlines()
creates = {re.search(r'CREATE TABLE "(\w+)"', l).group(1): l for l in lines if l.startswith("CREATE TABLE")}
rows = {t: [] for t in creates}
for l in lines:
    m = re.match(r'INSERT INTO "(\w+)" VALUES (.*);$', l)
    if m:
        rows[m.group(1)] += re.findall(r"\((?:[^()']|'[^']*')*\)", m.group(2))
qi = next(i for i, l in enumerate(lines) if l.startswith("SELECT count"))
query = "\n".join(lines[qi:])
m = re.match(r"(SELECT count\(\*\) FROM \(SELECT DISTINCT .*?)\nFROM (.*?)\nWHERE (.*)\) x;$", query, re.S)
select, from_s, where_s = m.group(1), m.group(2), m.group(3)
aliases = [a.strip() for a in from_s.split(",")]            # '"Op" t0'
conds = [c.strip() for c in where_s.split("\n  AND ")]
alias_of = lambda item: item.split()[-1]
select_aliases = set(re.findall(r"\b(t\d+)\.", select))

def mentions(cond, al):
    return re.search(rf"\b{al}\.", cond) is not None

def render(rows, aliases, conds):
    used = {item.split()[0].strip('"') for item in aliases}
    out = [creates[t] for t in creates if t in used]
    for t in creates:
        if t not in used: continue
        rs = rows[t]
        for i in range(0, len(rs), 500):
            out.append(f'INSERT INTO "{t}" VALUES ' + ", ".join(rs[i:i+500]) + ";")
    where = ("\nWHERE " + "\n  AND ".join(conds)) if conds else ""
    out.append(f"{select}\nFROM {', '.join(aliases)}{where}) x;")
    return "\n".join(out) + "\n"

def crashes(rows, aliases, conds):
    sql = render(rows, aliases, conds)
    p = subprocess.run(["podman", "run", "--rm", "-i", "--entrypoint", "sh", IMAGE, "-c",
                        "timeout 60 umbra-sql -createdb /tmp/r.db >/dev/null 2>&1; echo rc=$?"],
                       input=sql, capture_output=True, text=True)
    m = re.search(r"rc=(\d+)", p.stdout)
    rc = int(m.group(1)) if m else 0
    return rc >= 128

def ddmin(items, test, what):
    n = 2
    while len(items) >= 2:
        chunk = -(-len(items) // n)
        subsets = [items[i:i+chunk] for i in range(0, len(items), chunk)]
        cands = subsets + [[x for j, s in enumerate(subsets) if j != i for x in s] for i in range(len(subsets))]
        cands = [c for c in cands if c and len(c) < len(items)]
        results = list(POOL.map(test, cands))
        hit = next((c for c, r in zip(cands, results) if r), None)
        if hit is not None:
            items = hit
            n = 2 if len(hit) <= chunk else max(n - 1, 2)
            log(f"{what}: {len(items)} left")
        else:
            if n >= len(items): break
            n = min(len(items), n * 2)
    return items

assert crashes(rows, aliases, conds), "the full reproduction does not crash"
log("full reproduction crashes:", len(aliases), "aliases,", len(conds), "conditions,", sum(map(len, rows.values())), "rows")

# (1) conditions
conds = ddmin(conds, lambda cs: crashes(rows, aliases, cs), "conditions")
# drop aliases that nothing mentions any more
keep = [a for a in aliases if alias_of(a) in select_aliases or any(mentions(c, alias_of(a)) for c in conds)]
if len(keep) < len(aliases) and crashes(rows, keep, conds):
    aliases = keep
log("after conditions:", len(aliases), "aliases,", len(conds), "conditions")

# (2) aliases (each with the conditions that mention it); the selected alias stays
fixed = [a for a in aliases if alias_of(a) in select_aliases]
movable = [a for a in aliases if a not in fixed]
def with_aliases(sub):
    al = fixed + sub
    names = {alias_of(a) for a in al}
    cs = [c for c in conds if all(x in names for x in re.findall(r"\b(t\d+)\.", c))]
    return al, cs
movable = ddmin(movable, lambda sub: crashes(rows, *with_aliases(sub)), "aliases")
aliases, conds = with_aliases(movable)
log("after aliases:", len(aliases), "aliases,", len(conds), "conditions")

# (3) rows per used table
used = {a.split()[0].strip('"') for a in aliases}
rows = {t: rs for t, rs in rows.items() if t in used}
for t in sorted(rows, key=lambda t: -len(rows[t])):
    rows[t] = ddmin(rows[t], lambda rs, t=t: crashes({**rows, t: rs}, aliases, conds), f"rows of {t}")
    log(f"rows of {t}: {len(rows[t])}")

assert crashes(rows, aliases, conds)
(HERE / "repro-min.sql").write_text(render(rows, aliases, conds))
log("DONE: wrote repro-min.sql")
