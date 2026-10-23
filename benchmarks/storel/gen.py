#!/usr/bin/env python3
"""Generate the STOREL benchmark: <kernel>.egg, egglog-storel-backoff/<kernel>.egg, egg/<kernel>.rs.

STOREL (Schleich, Shaikhha, Suciu, SIGMOD'23, "Optimizing Tensor Programs on Flexible
Storage") optimizes sparse tensor kernels written in SDQL with equality saturation in egg.
The inputs in inputs/ are the ten kernels of its evaluation (five kernels x two
optimization stages) and the rules are the 44-rule "fine-grained" set, both taken from
the egg 0.6 implementation shipped with the slotted e-graphs artifact
(github.com/memoryleak47/slotted-egraphs-artifact, sdql/baseline, commit 83f2e5be).
Variables are de Bruijn indices (%0 is the innermost binder; sum binds key %1 and value
%0; merge binds key1 %2, val1 %1, key2 %0; let and lambda bind %0).

STOREL's beta reduction and index shifting are not rewrite rules: an e-class analysis keeps
the smallest known term of every class (`beta_extract`), and custom appliers substitute or
shift that one term in Rust and add the result. The egglog programs do the same with the
extension of systems/egglog-storel: `best : Term -> Frozen` computes the smallest term of
every class by rules (`Frozen` is the extension's base sort of immutable term trees, and
`:merge (frozen-min old new)` keeps the smaller term by (size, structural hash)), and the
extension's `fsubst`/`fshift` primitives substitute or shift it; `thaw` adds the result to
the e-graph. STOREL breaks size ties by keeping whichever term its e-graph happened to see
first, which depends on the engine's internal order; the hash HASH (computed in the
extension) makes the choice canonical. `thaw` writes the e-graph, which egglog admits only
outside semi-naive rules; the rules calling it are `:unsafe-seminaive` because their
actions depend on nothing but the query-bound, immutable frozen terms (see storel_rules).
`free : Term -> i64` is the free-variable bit set (union over the class, as in STOREL) used
by the side conditions. The analysis rules form the `aux` ruleset, saturated after every
step of the STOREL rules, which makes the analysis as up to date at the start of each
iteration as egg's eager analysis. egg applies the matches of one iteration rule by rule
with the analysis updated in between, so STOREL's shifting rules (which follow `beta` in the
rule list) see the beta-reduced term of a `let` the same iteration beta fires on it; without
that ordering, `sum-sum-vert-fuse` keeps wrapping its own output in new lets and the e-graph
never saturates. Each iteration is therefore two steps, `phase1` (the rules up to and
including beta) and `phase2` (the rest), each followed by the aux saturation.

"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# harness name -> (input file, node limit or None = run to saturation). The limits are below
# every variant's saturation size: on mmm-sum-2nd egg saturates at 14K e-nodes (0.6) or more,
# the egglog ports at 16K; on mmm-2nd egg's SimpleScheduler saturates at 179K, egg 0.6 at
# 206K, the egglog ports at 312K; the other stage-2 kernels and batax-1st never saturate (egg
# reaches 3-4.5M e-nodes before its memory limit).
KERNELS = {
    "mmm-sum-1st": ("mmm_sum_1st", None),
    "mttkrp-1st": ("mttkrp_1st", None),
    "mmm-1st": ("mmm_1st", None),
    "ttm-1st": ("ttm_1st", None),
    "batax-1st": ("batax_1st", 1_000_000),
    "mmm-sum-2nd": ("mmm_sum_2nd", 10_000),
    "mttkrp-2nd": ("mttkrp_2nd", 1_000_000),
    "mmm-2nd": ("mmm_2nd", 150_000),
    "ttm-2nd": ("ttm_2nd", 1_000_000),
    "batax-2nd": ("batax_2nd", 1_000_000),
}

# SDQL operator in the inputs -> egglog constructor, and how many variables each child binds.
OPS = {
    "*": ("Mul", (0, 0)), "+": ("Add", (0, 0)), "-": ("Sub", (0, 0)), "==": ("Eq", (0, 0)),
    "let": ("Let", (0, 1)), "sing": ("Sing", (0, 0)), "ifthen": ("IfThen", (0, 0)),
    "get": ("Get", (0, 0)), "sum": ("Sum", (0, 2)), "merge": ("Merge", (0, 0, 3)),
    "range": ("Range", (0, 0)), "subarray": ("SubArray", (0, 0, 0)), "apply": ("App", (0, 0)),
    "binop": ("Binop", (0, 0, 0)), "lambda": ("Lambda", (1,)), "unique": ("Unique", (0,)),
}
CONSTRUCTORS = {name: binders for name, binders in OPS.values()}  # egglog name -> binders
LEAVES = ["Var", "Num", "Sym"]  # (Var i64) de Bruijn index, (Num i64), (Sym String)
TERM_TABLES = LEAVES + list(CONSTRUCTORS)
SYMBOLS = ["mult", "add", "sub", "getf", "singf", "uniquef"]  # the only (Sym s) the rules create

# HASH: the structural hash that breaks size ties, computed by the extension (crate/src/lib.rs
# of systems/egglog-storel) for its Frozen terms: h(node) = (op * 4099 + sum_i h(child_i) *
# P[i]) mod 2^48 with P = [8191, 12281, 16381] and op = 1 + index of the constructor in
# TERM_TABLES; the leaf "children" are the index of a Var, the low 32 bits of a Num and the
# index of a Sym in SYMBOLS.


# ---------------------------------------------------------------- inputs -> de Bruijn terms

def parse_sexp(text: str):
    tokens = text.replace("(", " ( ").replace(")", " ) ").split()
    pos = 0

    def read():
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        if tok == "(":
            items = []
            while tokens[pos] != ")":
                items.append(read())
            pos += 1
            return items
        return tok

    expr = read()
    assert pos == len(tokens), "trailing input"
    return expr


def nameless(sexp, scope=()):
    """The input's named binders (`sum k v R body`, `merge k1 k2 v1 R S body`, `let x e body`,
    `lambda x body`) as de Bruijn terms: ("Var", i) / ("Num", n) / ("Sym", s) / (op, children)."""
    if isinstance(sexp, str):
        return ("Num", int(sexp)) if sexp.lstrip("-").isdigit() else ("Sym", sexp)
    head, *args = sexp
    if head == "var":
        (name,) = args
        return ("Var", list(reversed(scope)).index(name))
    if head == "let":
        x, e, body = args
        return ("Let", [nameless(e, scope), nameless(body, scope + (x,))])
    if head == "sum":
        k, v, r, body = args
        return ("Sum", [nameless(r, scope), nameless(body, scope + (k, v))])
    if head == "merge":
        k1, k2, v1, r, s, body = args
        return ("Merge", [nameless(r, scope), nameless(s, scope), nameless(body, scope + (k1, v1, k2))])
    if head == "lambda":
        x, body = args
        return ("Lambda", [nameless(body, scope + (x,))])
    name, binders = OPS[head]
    assert len(args) == len(binders), sexp
    return (name, [nameless(a, scope) for a in args])


def egglog_term(t) -> str:
    kind, x = t
    if kind == "Var":
        return f"(Var {x})"
    if kind == "Num":
        return f"(Num {x})"
    if kind == "Sym":
        return f'(Sym "{x}")'
    return "(" + " ".join([kind] + [egglog_term(c) for c in x]) + ")"


def egg_term(t) -> str:
    """The same term in the egg port's syntax (STOREL's operator names, %i variables)."""
    kind, x = t
    if kind == "Var":
        return f"%{x}"
    if kind in ("Num", "Sym"):
        return str(x)
    op = next(op for op, (name, _) in OPS.items() if name == kind)
    return "(" + " ".join([op] + [egg_term(c) for c in x]) + ")"


# ---------------------------------------------------------------- the egglog program

def datatype() -> str:
    lines = ["(datatype Term", "  (Var i64)", "  (Num i64)", "  (Sym String)"]
    for c, binders in CONSTRUCTORS.items():
        lines.append(f"  ({c} {' '.join(['Term'] * len(binders))})")
    return "\n".join(lines) + ")\n"


def vars_(n: int, p: str = "c") -> list[str]:
    return [f"{p}{i}" for i in range(n)]


def analysis_rules() -> str:
    """best and free for every Term constructor: every node submits its candidate term and
    best's :merge (frozen-min) keeps the (size, hash) minimum."""
    leaf = lambda pattern, fterm, free: f"(rule ((= e {pattern})) ((set (best e) {fterm}) (set (free e) {free})) :ruleset aux)"
    out = [leaf("(Var i)", "(fvar i)", "(<< 1 i)"), leaf("(Num n)", "(fnum n)", "0"), leaf("(Sym s)", "(fsym s)", "0")]
    for c, binders in CONSTRUCTORS.items():
        cs = vars_(len(binders))
        node = f"(= e ({c} {' '.join(cs)}))"
        bests = " ".join(f"(= f{i} (best {v}))" for i, v in enumerate(cs))
        out.append(f"(rule ({node} {bests}) ((set (best e) (f{len(cs)} \"{c}\" {' '.join(f'f{i}' for i in range(len(cs)))}))) :ruleset aux)")
        frees = " ".join(f"(= v{i} (free {v}))" for i, v in enumerate(cs))
        shifted = [f"(>> v{i} {b})" if b else f"v{i}" for i, b in enumerate(binders)]
        union = shifted[0]
        for sh in shifted[1:]:
            union = f"(| {union} {sh})"
        out.append(f"(rule ({node} {frees}) ((set (free e) {union})) :ruleset aux)")
    return "\n".join(out) + "\n"


def closed(v: str) -> str:
    """STOREL's side condition `!contains_ident(v, 0) && !contains_ident(v, 1)`: neither %0 nor
    %1 is free in v (a lookup and a filter; a relation could not be retracted when free grows)."""
    return f"(= fv_{v} (free {v})) (= (& fv_{v} 3) 0)"


# STOREL's 44 fine-grained rules (sdqlrules.rs `rules()`, same order). Plain rewrites are
# (name, lhs, rhs); extraction-based rules are (name, lhs, conditions, shifts, rhs) where
# shifts = [(var, cutoff, delta)] names the pattern variables whose best term is shifted by
# `delta` above `cutoff` (STOREL's with_shifted_*), and `rhs` uses `<var>'` for the result.
# `closed(v)` is STOREL's `!contains_ident(v, 0) && !contains_ident(v, 1)`.
RULES = [
    # algebraic rules
    ("mult-assoc1", "(Mul (Mul a b) c)", "(Mul a (Mul b c))"),
    ("mult-assoc2", "(Mul a (Mul b c))", "(Mul (Mul a b) c)"),
    ("sub-identity", "(Sub e e)", "(Num 0)"),
    ("add-zero", "(Add e (Num 0))", "e"),
    ("sub-zero", "(Sub e (Num 0))", "e"),
    ("eq-comm", "(Eq a b)", "(Eq b a)"),
    # normalize constructs to unary and binary operators
    ("mult-app1", "(Mul a b)", '(Binop (Sym "mult") a b)'),
    ("mult-app2", '(Binop (Sym "mult") a b)', "(Mul a b)"),
    ("add-app1", "(Add a b)", '(Binop (Sym "add") a b)'),
    ("add-app2", '(Binop (Sym "add") a b)', "(Add a b)"),
    ("sub-app1", "(Sub a b)", '(Binop (Sym "sub") a b)'),
    ("sub-app2", '(Binop (Sym "sub") a b)', "(Sub a b)"),
    ("get-app1", "(Get a b)", '(Binop (Sym "getf") a b)'),
    ("get-app2", '(Binop (Sym "getf") a b)', "(Get a b)"),
    ("sing-app1", "(Sing a b)", '(Binop (Sym "singf") a b)'),
    ("sing-app2", '(Binop (Sym "singf") a b)', "(Sing a b)"),
    ("unique-app1", "(Unique a)", '(App (Sym "uniquef") a)'),
    ("unique-app2", '(App (Sym "uniquef") a)', "(Unique a)"),
    # let-floating for binary and unary operators
    ("let-binop3", "(Let e1 (Binop f e2 e3))", "(Binop f (Let e1 e2) (Let e1 e3))"),
    ("let-binop4", "(Binop f (Let e1 e2) (Let e1 e3))", "(Let e1 (Binop f e2 e3))"),
    ("let-apply1", "(Let e1 (App e2 e3))", "(App e2 (Let e1 e3))"),
    ("let-apply2", "(App e2 (Let e1 e3))", "(Let e1 (App e2 e3))"),
    # if-then-else
    ("if-mult2", "(Mul e1 (IfThen e2 e3))", "(IfThen e2 (Mul e1 e3))"),
    ("if-to-mult", "(IfThen e1 e2)", "(Mul e1 e2)"),
    ("mult-to-if", "(Mul (Eq e1_1 e1_2) e2)", "(IfThen (Eq e1_1 e1_2) e2)"),
    # beta-reduction: substitute the smallest term of e for %0 in the smallest term of body
    ("beta", "(Let e body)", "", "subst", "body'"),
    # sum-factorization
    ("sum-fact-1", "(Sum R (Mul e1 e2))", closed("e1"), [("e1", 2, -2)], "(Mul e1' (Sum R e2))"),
    ("sum-fact-2", "(Sum R (Mul e1 e2))", closed("e2"), [("e2", 2, -2)], "(Mul (Sum R e1) e2')"),
    ("sum-fact-3", "(Sum R (Sing e1 e2))", closed("e1"), [("e1", 2, -2)], "(Sing e1' (Sum R e2))"),
    # sing-mult associativity
    ("sing-mult-1", "(Sing e1 (Mul e2 e3))", "(Mul (Sing e1 e2) e3)"),
    ("sing-mult-2", "(Sing e1 (Mul e2 e3))", "(Mul e2 (Sing e1 e3))"),
    ("sing-mult-3", "(Mul (Sing e1 e2) e3)", "(Sing e1 (Mul e2 e3))"),
    ("sing-mult-4", "(Mul e2 (Sing e1 e3))", "(Sing e1 (Mul e2 e3))"),
    # sum-defactorization
    ("sum-fact-inv-1", "(Mul e1 (Sum R e2))", "", [("e1", 0, 2)], "(Sum R (Mul e1' e2))"),
    ("sum-fact-inv-3", "(Sing e1 (Sum R e2))", "", [("e1", 0, 2)], "(Sum R (Sing e1' e2))"),
    # sum-sum vertical fusion
    ("sum-sum-vert-fuse-1", "(Sum (Sum R (Sing (Var 1) body1)) body2)", "",
     [("body1", 0, 1), ("body2", 2, 2)], "(Sum R (Let (Var 1) (Let body1' body2')))"),
    ("sum-sum-vert-fuse-2", "(Sum (Sum R (Sing (Unique key) body1)) body2)", "",
     [("body1", 0, 1), ("body2", 2, 2)], "(Sum R (Let (Unique key) (Let body1' body2')))"),
    # sum-range vertical fusion
    ("sum-range-1", "(Sum (Range st en) (IfThen (Eq (Var 0) key) body))", "", [("st", 0, 2)],
     "(Sum (Range st en) (IfThen (Eq (Var 1) (Sub key (Sub st' (Num 1)))) body))"),
    # nested sum to merge conversion
    ("sum-merge", "(Sum R (Sum S (IfThen (Eq (Var 2) (Var 0)) body)))", "", [("S", 2, -2)],
     "(Merge R S' (Let (Var 1) body))"),
    # get and sum conversion
    ("get-to-sum", "(Get dict key)", "", [("key", 0, 2)], "(Sum dict (IfThen (Eq (Var 1) key') (Var 0)))"),
    ("sum-to-get", "(Sum R (IfThen (Eq (Var 1) body2) body1))", closed("body2"),
     [("R", 0, 1), ("body2", 2, -2)], "(Let body2' (Let (Get R' (Var 0)) body1))"),
    ("get-range", "(Get (Range st en) idx)", "(Add idx (Sub st (Num 1)))"),
    # sum simplifier
    ("sum-sing", "(Sum e1 (Sing (Var 1) (Var 0)))", "e1"),
    # annotation removal
    ("unique-rm", "(Unique e)", "e"),
]


def storel_rules() -> str:
    out = []
    ruleset = "phase1"
    for rule in RULES:
        name = rule[0]
        if len(rule) == 3:
            _, lhs, rhs = rule
            out.append(f"(rewrite {lhs} {rhs} :ruleset {ruleset})  ; {name}")
            continue
        _, lhs, conds, shifts, rhs = rule
        # The extension computes the term in Rust and thaw adds it to the e-graph. egglog admits
        # such primitives only outside semi-naive rules; :unsafe-seminaive keeps semi-naive
        # evaluation on our guarantee that the action depends on nothing but its query-bound
        # values (Frozen values are immutable), so the rule re-fires exactly when a best term
        # changes, which is when egg's applier (re-run on every match every iteration) would
        # add something new. (:naive re-ran every match each step and dominated the back-off
        # runs: 104 of 150 s on batax-2nd.)
        if shifts == "subst":  # beta: substitute the smallest term of e for %0 in the one of body
            computed = [("body", "(fsubst 0 f_e f_body)")]
            bests = "(= f_e (best e)) (= f_body (best body))"
        else:
            computed = [(v, f"(fshift {c} {d} f_{v})") for v, c, d in shifts]
            bests = " ".join(f"(= f_{v} (best {v}))" for v, _, _ in shifts)
        result = rhs
        for v, expr in computed:
            result = result.replace(f"{v}'", f"(thaw {expr})")
        out.append(f"(rule ((= lhs {lhs}) {conds} {bests})\n"
                   f"      ((union lhs {result})) :ruleset {ruleset} :unsafe-seminaive)  ; {name}")
        if name == "beta":
            ruleset = "phase2"
    return "\n".join(out) + "\n"


def program(kernel: str, backoff: bool = False) -> str:
    """The egglog program for the extension of systems/egglog-storel; `backoff` adds
    egglog-experimental's back-off scheduler (the egglog-storel-backoff/ ports)."""
    stem, limit = KERNELS[kernel]
    term = nameless(parse_sexp((HERE / "inputs" / f"{stem}.sexp").read_text()))
    head = (f";; STOREL kernel {kernel}: generated by gen.py from inputs/{stem}.sexp; do not edit.\n"
            ";; The smallest terms are values of the egglog-storel extension's Frozen base sort; beta reduction and\n"
            ";; shifting are its fsubst/fshift primitives, thaw adds the result (systems/egglog-storel/crate).\n")
    if backoff:
        head += ";; egglog-storel-backoff port: the 44 rules run under the back-off scheduler (STOREL's egg configuration).\n"
    body = (
        head + "\n" + datatype() + "\n"
        + "(ruleset phase1)  ; STOREL's rules up to and including beta\n(ruleset phase2)  ; the rules after beta\n"
        + "(ruleset aux)     ; the analysis\n\n"
        + "(function best (Term) Frozen :merge (frozen-min old new))  ; the smallest known term, by (size, HASH)\n"
        + "(function free (Term) i64 :merge (| old new))      ; free de Bruijn indices (bit set), union over the class\n\n"
        + analysis_rules() + "\n" + storel_rules() + "\n"
        + f"(let $root {egglog_term(term)})\n\n"
    )
    if backoff:
        body += "(let-scheduler bo (back-off))  ; match limit 1000, ban length 5, as egg's BackoffScheduler::default()\n"
    run = "(run-with bo {})" if backoff else "(run {})"
    step = f"{run.format('phase1')} (saturate (run aux)) {run.format('phase2')} (saturate (run aux))"
    body += "(run-schedule (saturate (run aux)))  ; the analysis of the start term\n"
    if limit is None:
        body += f"(run-schedule (saturate {step}))\n"
    else:
        body += (f";; BENCHMARK_NODE_LIMIT {limit} {' '.join(TERM_TABLES)}\n"
                 f"(run-schedule {step})\n")
    return body


def egg_wrapper(kernel: str) -> str:
    stem, limit = KERNELS[kernel]
    term = nameless(parse_sexp((HERE / "inputs" / f"{stem}.sexp").read_text()))
    limit = "None" if limit is None else f"Some({limit})"
    return (f"//! egg port of storel/{kernel}; see storel.rs.in (generated by gen.py; do not edit).\n"
            f'const KERNEL: &str = "{kernel}";\n'
            f'const START: &str = "{egg_term(term)}";\n'
            f"const NODE_LIMIT: Option<usize> = {limit};\n"
            f'include!("storel.rs.in");\n')


def main() -> None:
    if sys.argv[1:] == ["--egg-terms"]:  # for cross-checking the conversion against STOREL's
        for kernel, (stem, _) in KERNELS.items():
            print(stem, egg_term(nameless(parse_sexp((HERE / "inputs" / f"{stem}.sexp").read_text()))))
        return
    for d in ("egglog-storel-backoff", "egg"):
        (HERE / d).mkdir(exist_ok=True)
    for kernel in KERNELS:
        (HERE / f"{kernel}.egg").write_text(program(kernel))
        (HERE / "egglog-storel-backoff" / f"{kernel}.egg").write_text(program(kernel, backoff=True))
        (HERE / "egg" / f"{kernel}.rs").write_text(egg_wrapper(kernel))


if __name__ == "__main__":
    main()
