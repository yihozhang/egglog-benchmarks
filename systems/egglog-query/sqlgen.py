#!/usr/bin/env python3
"""Translate one egglog rule into the query-only benchmark artifacts.

Given the schema exported by the driver (schema.json) and the text of a rule, produce
  - the egglog test file: (relation R (...)) (ruleset q) (rule <LHS> ((R v...)) :ruleset q :naive) (run q 1)
    where v... are the variables of the original RHS that the LHS binds, in order of appearance;
  - the SQL query: CREATE TEMP TABLE r AS SELECT DISTINCT v... FROM <one alias per LHS atom> WHERE ...
  - CREATE TABLE statements for the tables it touches (functional dependency as PRIMARY KEY).

Table layout (as exported): constructor/function f(i0..in-1) -> out has columns c0..cn-1, "out";
relations (output sort "@...Sort") have no out column. With subsumption=True every table has a
boolean column "live" (= not subsumed) and every atom requires live = true; otherwise subsumed rows
are simply not loaded. No timestamp columns.
"""

import json
import re

ARITH = {"+": "+", "-": "-", "*": "*", "/": "/", "%": "%"}  # i64 arithmetic (truncating, as in Rust)
CMP = {"<": "<", ">": ">", "<=": "<=", ">=": ">="}  # i64 comparisons (as LHS facts)
PRIM = {"i64": "BIGINT", "f64": "DOUBLE PRECISION", "String": "VARCHAR", "bool": "BOOLEAN"}


def parse_sexp(text):
    toks = re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()]+', re.sub(r";[^\n]*", "", text))
    pos = 0

    def go():
        nonlocal pos
        t = toks[pos]; pos += 1
        if t == "(":
            out = []
            while toks[pos] != ")":
                out.append(go())
            pos += 1
            return out
        return t
    return go()


def is_var(x):
    return (isinstance(x, str) and not x.startswith('"') and x not in ("true", "false")
            and not re.fullmatch(r"-?\d+(\.\d+)?", x))


def literal(x):
    if x in ("true", "false"):
        return x.upper(), "bool"
    if x.startswith('"'):
        return "'" + x[1:-1].replace("'", "''") + "'", "String"
    if "." in x:
        # explicit type: Umbra reads 0.044715 as an exact decimal that never equals the stored double
        return f"CAST({x} AS DOUBLE PRECISION)", "f64"
    return x, "i64"


class Schema:
    def __init__(self, path):
        self.t = {t["name"]: t for t in json.load(open(path))}

    def has_out(self, name):
        out = self.t[name]["output"]
        return not (out.startswith("@") or out == "Unit")  # relations: @RSort (constructor) or Unit (function)

    def ncols(self, name):
        return len(self.t[name]["inputs"])

    def create_table(self, name, subsumption):
        t = self.t[name]
        cols = [f'c{i} {PRIM.get(s, "BIGINT")} NOT NULL' for i, s in enumerate(t["inputs"])]
        keys = [f"c{i}" for i in range(len(t["inputs"]))]
        if self.has_out(name):
            cols.append(f'"out" {PRIM.get(t["output"], "BIGINT")} NOT NULL')
        if not keys:  # nullary: at most one row
            keys = ['"out"'] if self.has_out(name) else []
        if subsumption:
            cols.append("live BOOLEAN NOT NULL")
        pk = f", PRIMARY KEY ({', '.join(keys)})" if keys else ""
        if not self.has_out(name) and t["inputs"]:
            pk = f", PRIMARY KEY ({', '.join(f'c{i}' for i in range(len(t['inputs'])))})"
        return f'CREATE TABLE "{name}" ({", ".join(cols)}{pk});'


def translate(schema, rule_text, name, subsumption=False):
    rule = parse_sexp(rule_text)
    assert rule[0] == "rule", rule[0]
    lhs, rhs = rule[1], rule[2]
    atoms = []        # (alias, table)
    conds = []
    var_expr = {}     # var -> SQL expression
    var_sort = {}

    def bind(v, expr, sort):
        if v in var_expr:
            conds.append(f"{var_expr[v]} = {expr}")
        else:
            var_expr[v], var_sort[v] = expr, sort

    def term(x, sort_hint=None):
        """SQL expression (and sort) for a term; calls become atoms."""
        if isinstance(x, list):
            f, args = x[0], x[1:]
            if f in ARITH and len(args) == 2:
                ea, sa = term(args[0]); eb, _ = term(args[1])
                if ea is None or eb is None:
                    raise ValueError(f"unbound operand of {f}")
                if f in ("/", "%"):
                    # egglog's primitive fails on a zero divisor (no match); NULL makes SQL agree
                    return f"(CASE WHEN {eb} <> 0 THEN {ea} {ARITH[f]} {eb} END)", sa
                return f"({ea} {ARITH[f]} {eb})", sa
            if f not in schema.t:
                raise ValueError(f"unsupported primitive or unknown table {f}")
            t = schema.t[f]
            alias = f"t{len(atoms)}"
            atoms.append((alias, f))
            if subsumption:
                conds.append(f"{alias}.live")
            for i, (a, s) in enumerate(zip(args, t["inputs"])):
                e, _ = term(a, s)
                col = f"{alias}.c{i}"
                if isinstance(a, str) and is_var(a):
                    bind(a, col, s)
                else:
                    conds.append(f"{col} = {e}")
            return (f'{alias}."out"' if schema.has_out(f) else None), t["output"]
        if is_var(x):
            if x in var_expr:
                return var_expr[x], var_sort[x]
            return None, sort_hint
        return literal(x)

    def side(x):
        """Expression for one side of = / !=, binding a fresh variable if needed."""
        return term(x)

    for fact in lhs:
        if fact[0] == "=":
            a, b = fact[1], fact[2]
            if is_var(a) and a not in var_expr and isinstance(b, list):
                e, s = term(b); bind(a, e, s)
            elif is_var(b) and b not in var_expr and isinstance(a, list):
                e, s = term(a); bind(b, e, s)
            else:
                ea, sa = term(a); eb, sb = term(b)
                if ea is None and is_var(a):
                    bind(a, eb, sb)
                elif eb is None and is_var(b):
                    bind(b, ea, sa)
                else:
                    conds.append(f"{ea} = {eb}")
        elif fact[0] in CMP:
            ea, _ = term(fact[1]); eb, _ = term(fact[2])
            if ea is None or eb is None:
                raise ValueError(f"unbound operand of {fact[0]}")
            conds.append(f"{ea} {CMP[fact[0]]} {eb}")
        elif fact[0] == "!=":
            ea, _ = term(fact[1]); eb, _ = term(fact[2])
            assert ea is not None and eb is not None, fact
            conds.append(f"{ea} <> {eb}")
        else:
            term(fact)

    # variables of the original RHS that the LHS binds, in order of first appearance
    rhs_vars = []
    def walk(x):
        if isinstance(x, list):
            for y in x[1:] if x and x[0] in ("let",) else x:
                walk(y)
        elif is_var(x) and x in var_expr and x not in rhs_vars:
            rhs_vars.append(x)
    walk(rhs)

    rel = f"R_{name}"
    sorts = " ".join(var_sort[v] for v in rhs_vars)
    lhs_text = rule_text[rule_text.index("(", rule_text.index("(") + 1):]  # from the LHS list on
    # re-emit the LHS verbatim from the parsed form
    def emit(x):
        return "(" + " ".join(emit(y) for y in x) + ")" if isinstance(x, list) else x
    # The result goes into a function to i64 that is always set to 1 (not a relation: egglog's
    # relations mint a fresh id per inserted row, and duplicate inserts then union those ids).
    egg = (f"(function {rel} ({sorts}) i64 :no-merge)\n(ruleset q_{name})\n"
           f"(rule {emit(lhs)}\n      ((set ({rel} {' '.join(rhs_vars)}) 1))\n      :ruleset q_{name} :naive)\n"
           f"(run q_{name} 1)\n")
    cols = ", ".join(f'{var_expr[v]} AS "{v}"' for v in rhs_vars)
    sql = (f"CREATE TEMP TABLE r_{name} AS SELECT DISTINCT {cols}\nFROM "
           + ", ".join(f'"{t}" {a}' for a, t in atoms)
           + ("\nWHERE " + "\n  AND ".join(conds) if conds else "") + ";")
    tables = sorted({t for _, t in atoms})
    return {"egglog": egg, "sql": sql, "tables": tables, "rhs_vars": rhs_vars, "atoms": len(atoms)}
