#!/usr/bin/env python3
"""Query-only benchmark, egglog side, for every program of a suite.

Usage: run_suite.py <suite> <outdir> [program ...]
For each program: export its final state (driver export), translate every rule and rewrite
of the program text (and the files it includes) with sqlgen, and run every translated rule
in egglog with --no-decomp (driver run, 1 thread) for the paper configuration.
Writes <outdir>/<program>/{schema.json,tables/,queries/,index.json,egglog.jsonl}.
"""
import json, os, pathlib, re, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); import sqlgen
ROOT = HERE.parent.parent
DRIVER = pathlib.Path(os.environ.get("EGGLOG_QUERY_DRIVER", ROOT / "build/query/driver/target/release/egglog-query-driver"))

def program_text(path):
    text = path.read_text()
    for inc in re.findall(r'\(include "([^"]+)"\)', text):
        text = text.replace(f'(include "{inc}")', program_text(path.parent / inc))
    return text

def rules_of(text):
    forms = sqlgen.parse_sexp("(" + text + ")")
    emit = lambda x: "(" + " ".join(emit(y) for y in x) + ")" if isinstance(x, list) else x
    opt = lambda f, k: f[f.index(k) + 1] if k in f else None
    out = []
    for f in forms:
        if not (isinstance(f, list) and f): continue
        if f[0] == "rule":
            out.append((opt(f, ":name"), opt(f, ":ruleset"), emit(["rule", f[1], f[2]])))
        elif f[0] in ("rewrite", "birewrite"):
            when = list(opt(f, ":when") or [])
            out.append((None, opt(f, ":ruleset"), emit(["rule", [["=", "__root", f[1]]] + when, [["union", "__root", f[2]]]])))
            if f[0] == "birewrite":
                out.append((None, opt(f, ":ruleset"), emit(["rule", [["=", "__root", f[2]]] + when, [["union", "__root", f[1]]]])))
    return out

def run(suite, outdir, programs):
    no_decomp_only = os.environ.get("EGGLOG_QUERY_NO_DECOMP_ONLY") == "1"
    modes = [True] if no_decomp_only else [False, True]
    sdir = ROOT / "benchmarks" / suite
    programs = programs or sorted(p.stem for p in sdir.glob("*.egg") if "header" not in p.stem)
    for prog in programs:
        out = outdir / prog; (out / "queries").mkdir(parents=True, exist_ok=True)
        if not (out / "schema.json").exists():
            subprocess.run([str(DRIVER), "export", f"{prog}.egg", str(out),
                            *(["--no-decomp"] if no_decomp_only else [])], cwd=sdir, check=True)
        schema = sqlgen.Schema(out / "schema.json")
        index, failed = [], []
        for i, (name, ruleset, text) in enumerate(rules_of(program_text(sdir / f"{prog}.egg"))):
            qid = f"r{i}"
            try:
                t = sqlgen.translate(schema, text, qid)
            except Exception as e:
                failed.append({"id": qid, "error": str(e), "text": text}); continue
            if not t["rhs_vars"]:
                failed.append({"id": qid, "error": "no RHS variables", "text": text}); continue
            for ext, body in (("egg", t["egglog"]), ("sql", t["sql"]), ("tables.json", json.dumps(t["tables"]))):
                (out / "queries" / f"{qid}.{ext}").write_text(body)
            index.append({"id": qid, "name": name or text[:90], "ruleset": ruleset, "atoms": t["atoms"], "text": text})
        (out / "index.json").write_text(json.dumps({"queries": index, "untranslated": failed}, indent=1))
        tests = [str(out / "queries" / f"{q['id']}.egg") for q in index]
        with open(out / "egglog.jsonl", "w") as f:
            for no_decomp in modes:
                cmd = [str(DRIVER), "run", f"{prog}.egg", "-j", "1",
                       *(["--no-decomp"] if no_decomp else []), *tests]
                p = subprocess.run(cmd, cwd=sdir, capture_output=True, text=True)
                (out / f"egglog-command-{no_decomp}.json").write_text(json.dumps(cmd, indent=2))
                (out / f"egglog-stderr-{no_decomp}.txt").write_text(p.stderr)
                p.check_returncode()
                for line in p.stdout.splitlines():
                    r = json.loads(line); r["no_decomp"] = no_decomp; r["query"] = pathlib.Path(r["test"]).stem
                    r["engine_label"] = "egglog-cracked" if no_decomp_only else "egglog"
                    f.write(json.dumps(r) + "\n")
        print(f"{prog}: {len(index)} queries, {len(failed)} untranslated", flush=True)

if __name__ == "__main__":
    run(sys.argv[1], pathlib.Path(sys.argv[2]).resolve(), sys.argv[3:])
