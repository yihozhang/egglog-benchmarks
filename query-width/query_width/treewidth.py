"""Static relational-query hypergraph treewidth analysis for egglog programs.

Run with ``python -m query_width.treewidth --help``. This analyzes logical query
hypergraphs, excluding rewrite conditions and built-in predicates/computations.
The default width is the treewidth of the hypergraph's primal graph.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import platform
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import networkx as nx
from networkx.algorithms.approximation import treewidth_min_degree, treewidth_min_fill_in

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITES = ("luminal", "eggcc", "hardboiled")
TOKEN = re.compile(r'\s+|;[^\n]*|"(?:\\.|[^"\\])*"|[()]|[^\s();"]+', re.DOTALL)
NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z')
# These primitives return Unit on success, not a boolean or a fresh result.
UNIT_PRIMITIVES = frozenset({"!=", "<", "<=", ">", ">=", "value-eq"} | {
    f"{container}-{operation}"
    for container in ("vec", "set", "map", "multiset")
    for operation in ("contains", "not-contains")
} | {"multiset-contains-swapped", "multiset-not-contains-swapped"})


class SourceError(ValueError):
    """A source could not be analyzed; never silently skip a broken input."""


@dataclass(frozen=True)
class SList:
    items: tuple[Expr, ...]
    line: int = field(default=1, compare=False)
    offset: int = field(default=0, compare=False)


Expr = str | SList


def parse_program(text: str, source: str = "<string>") -> Iterator[SList]:
    """Parse balanced S-expressions, preserving strings, comments and locations.

    The stack is iterative so large/deep initialization terms do not exhaust
    Python's recursion limit. Semicolons inside strings are not comments.
    """
    stack: list[tuple[list[Expr], int, int]] = []
    pos, line = 0, 1
    while pos < len(text):
        match = TOKEN.match(text, pos)
        if match is None:
            raise SourceError(f"{source}:{line}: unterminated string")
        token = match.group()
        token_line, token_pos = line, pos
        line += token.count("\n")
        pos = match.end()
        if token.isspace() or token.startswith(";"):
            continue
        if token == "(":
            stack.append(([], token_line, token_pos))
            continue
        if token == ")":
            if not stack:
                raise SourceError(f"{source}:{token_line}: unexpected ')'")
            items, start_line, start_pos = stack.pop()
            node: Expr = SList(tuple(items), start_line, start_pos)
        else:
            node = token
        if stack:
            stack[-1][0].append(node)
        elif isinstance(node, SList):
            yield node
        else:
            raise SourceError(f"{source}:{token_line}: expected a top-level command")
    if stack:
        raise SourceError(f"{source}:{stack[-1][1]}: unclosed '('")


def items(expr: Expr) -> tuple[Expr, ...]:
    if not isinstance(expr, SList):
        raise SourceError(f"expected a list, got {expr!r}")
    return expr.items


def symbol(expr: Expr) -> str:
    if not isinstance(expr, str) or expr.startswith('"'):
        raise SourceError(f"expected a symbol, got {expr!r}")
    return expr


def render(expr: Expr) -> str:
    return expr if isinstance(expr, str) else "(" + " ".join(map(render, expr.items)) + ")"


def string_value(expr: Expr) -> str:
    if not isinstance(expr, str) or not expr.startswith('"'):
        raise SourceError(f"expected a quoted string, got {expr!r}")
    try:
        return json.loads(expr, strict=False)
    except ValueError as exc:
        raise SourceError(f"invalid string {expr!r}: {exc}") from exc


@dataclass(frozen=True)
class Constant:
    kind: str
    value: str


UNIT = Constant("unit", "()")
Term = int | Constant


def literal(expr: str) -> Constant | None:
    if expr.startswith('"'):
        return Constant("string", string_value(expr))
    if expr in ("true", "false"):
        return Constant("bool", expr)
    if expr in ("NaN", "inf", "-inf"):
        return Constant("f64", expr)
    if NUMBER.fullmatch(expr):
        if re.fullmatch(r"[+-]?\d+", expr) and -(2**63) <= int(expr) < 2**63:
            return Constant("i64", str(int(expr)))
        value = float(expr)
        if math.isfinite(value):
            return Constant("f64", repr(value))
    return None


@dataclass
class Environment:
    # Declared relations have logical input columns only. Function/constructor
    # outputs contribute a column, except the unique Unit value.
    calls: dict[str, str] = field(default_factory=dict)
    globals: set[str] = field(default_factory=set)

    def declare(self, form: SList) -> None:
        parts = form.items
        if not parts:
            return
        head = symbol(parts[0])
        if head == "relation":
            self.calls[symbol(parts[1])] = "relation"
        elif head in ("function", "constructor"):
            self.calls[symbol(parts[1])] = "unit" if parts[3] == "Unit" else "function"
        elif head == "datatype":
            for variant in parts[2:]:
                if isinstance(variant, SList):
                    self.calls[symbol(variant.items[0])] = "function"
        elif head == "datatype*":
            for datatype in parts[1:]:
                d = items(datatype)
                # datatype* can contain explicit (sort Name (...)) entries.
                if d and d[0] != "sort":
                    for variant in d[1:]:
                        if isinstance(variant, SList):
                            self.calls[symbol(variant.items[0])] = "function"
        elif head in ("let", "constant"):
            self.globals.add(symbol(parts[1]))


def options(parts: tuple[Expr, ...]) -> dict[str, Expr | bool]:
    result: dict[str, Expr | bool] = {}
    flags = {":subsume", ":naive", ":unsafe-seminaive", ":no-decomp", ":internal-include-subsumed"}
    index = 0
    while index < len(parts):
        key = symbol(parts[index])
        if key in flags:
            result[key] = True
            index += 1
        elif key in (":name", ":ruleset", ":when") and index + 1 < len(parts):
            result[key] = parts[index + 1]
            index += 2
        else:
            raise SourceError(f"unsupported rule/rewrite option {key!r}")
    return result


@dataclass(frozen=True)
class Query:
    kind: str
    direction: str
    facts: tuple[Expr, ...]
    pattern: Expr | None
    name: str
    ruleset: str
    ignored_when_facts: int = 0


def extract_queries(form: SList) -> list[Query]:
    p = form.items
    if not p or p[0] not in ("rule", "rewrite", "birewrite"):
        return []
    if len(p) < 3:
        raise SourceError("rule/rewrite needs a body/left side and a head/right side")
    opts = options(p[3:])
    name = string_value(opts[":name"]) if ":name" in opts else ""
    ruleset = symbol(opts[":ruleset"]) if ":ruleset" in opts else ""
    if p[0] == "rule":
        items(p[2])  # Validate the action list, but never include it in the query.
        return [Query("rule", "forward", items(p[1]), None, name, ruleset)]
    conditions = items(opts[":when"]) if ":when" in opts else ()
    patterns = [("forward", p[1])]
    if p[0] == "birewrite":
        patterns.append(("reverse", p[2]))
    return [Query(symbol(p[0]), direction, (), pattern, name, ruleset, len(conditions))
            for direction, pattern in patterns]


@dataclass(frozen=True)
class Call:
    head: str
    kind: str
    inputs: tuple[Term, ...]
    output: Term


class QueryBuilder:
    def __init__(self, env: Environment):
        self.env = env
        self.variables: dict[str, int] = {}
        self.parent: dict[Term, Term] = {}
        self.calls: list[Call] = []
        self.next_id = 0
        self.ignored_facts: list[str] = []

    def fresh(self) -> int:
        var = self.next_id
        self.next_id += 1
        self.parent[var] = var
        return var

    def find(self, term: Term) -> Term:
        self.parent.setdefault(term, term)
        root = term
        while self.parent[root] != root:
            root = self.parent[root]
        while term != root:
            parent = self.parent[term]
            self.parent[term] = root
            term = parent
        return root

    def union(self, first: Term, second: Term) -> bool:
        a, b = self.find(first), self.find(second)
        if a == b:
            return False
        # Fixed values stay fixed. Different fixed values may make the query
        # unsatisfiable; this analysis does not decide query satisfiability.
        if isinstance(a, Constant) and isinstance(b, Constant):
            return False
        if isinstance(b, Constant) or (isinstance(a, int) and isinstance(b, int) and b < a):
            a, b = b, a
        self.parent[b] = a
        return True

    def term(self, expr: Expr) -> Term:
        if isinstance(expr, str):
            const = literal(expr)
            if const is not None:
                return const
            if expr in self.env.globals:
                return Constant("global", expr)
            if expr not in self.variables:
                self.variables[expr] = self.fresh()
            return self.variables[expr]
        if not expr.items:
            return UNIT
        head = symbol(expr.items[0])
        if head == "=":
            raise SourceError("equality is a fact, not a nested expression")
        args = tuple(self.term(arg) for arg in expr.items[1:])
        kind = self.env.calls.get(head, "primitive")
        unit = kind in ("relation", "unit") or (kind == "primitive" and head in UNIT_PRIMITIVES)
        output = UNIT if unit else self.fresh()
        self.calls.append(Call(head, kind, args, output))
        return output

    def build(self, query: Query) -> dict:
        if query.pattern is not None:
            self.term(query.pattern)
        for fact in query.facts:
            p = items(fact)
            if p and p[0] == "=":
                if len(p) != 3:
                    raise SourceError("equality fact must have two operands")
                # A scalar computation used as a condition is not a relational
                # match. Keep equality bindings to stored calls, and aliases.
                if any(self.is_primitive_root(x) for x in p[1:]) and not any(
                    isinstance(x, SList) and x.items and x.items[0] in self.env.calls for x in p[1:]
                ):
                    self.ignored_facts.append(render(fact))
                    continue
                self.union(self.term(p[1]), self.term(p[2]))
            elif self.is_primitive_root(fact):
                # Exclude the entire condition, including lookups inside it.
                self.ignored_facts.append(render(fact))
            else:
                self.term(fact)

        # Common calls denote the same result. Repeat after equality aliases
        # change input representatives, matching egglog's functional dependency
        # canonicalization. This is not injectivity or arbitrary simplification.
        changed = True
        while changed:
            changed = False
            seen: dict[tuple, Term] = {}
            for call in self.calls:
                key = (call.head, call.kind, tuple(self.find(x) for x in call.inputs))
                if key in seen:
                    changed |= self.union(seen[key], call.output)
                else:
                    seen[key] = call.output

        numbering: dict[int, int] = {}

        def encode(term: Term) -> list:
            term = self.find(term)
            if isinstance(term, Constant):
                return [term.kind, term.value]
            if term not in numbering:
                numbering[term] = len(numbering)
            return ["var", numbering[term]]

        atoms, seen_atoms = [], set()
        for call in self.calls:
            if call.kind == "primitive":
                continue
            terms = [*call.inputs]
            if call.output != UNIT:
                terms.append(call.output)
            atom = {"head": call.head, "kind": call.kind, "terms": [encode(t) for t in terms]}
            key = json.dumps(atom, sort_keys=True)
            if key not in seen_atoms:
                seen_atoms.add(key)
                atoms.append(atom)
        # Only variables in retained relational atoms belong to this query.
        hyperedges = [sorted({value for kind, value in atom["terms"] if kind == "var"}) for atom in atoms]
        signature = {"atoms": atoms, "variables": len(numbering), "hyperedges": hyperedges}
        query_id = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        return {"query_id": query_id, **signature, "ignored_body_facts": self.ignored_facts,
                "ignored_when_facts": query.ignored_when_facts,
                "ignored_primitive_calls": sum(c.kind == "primitive" for c in self.calls)}

    def is_primitive_root(self, expr: Expr) -> bool:
        return (isinstance(expr, SList) and bool(expr.items)
                and symbol(expr.items[0]) not in self.env.calls)


def query_graph(query: dict, model: str = "primal") -> nx.Graph:
    graph = nx.Graph()
    graph.add_nodes_from(range(query["variables"]))
    for index, variables in enumerate(query["hyperedges"]):
        if model == "primal":
            graph.add_edges_from(itertools.combinations(variables, 2))
        elif model == "incidence":
            node = query["variables"] + index
            graph.add_node(node)
            graph.add_edges_from((node, var) for var in variables)
        else:
            raise ValueError(f"unknown graph model {model!r}")
    return graph


def minor_min_width(graph: nx.Graph) -> int:
    """Lower bound: minimum degree of successively contracted graph minors."""
    adjacency = {v: set(graph[v]) for v in graph}
    lower = 0
    while adjacency:
        vertex = min(adjacency, key=lambda v: (len(adjacency[v]), v))
        neighbors = adjacency[vertex]
        lower = max(lower, len(neighbors))
        if neighbors:
            target = min(neighbors, key=lambda v: (len(adjacency[v]), v))
            for neighbor in neighbors:
                adjacency[neighbor].discard(vertex)
                if neighbor != target:
                    adjacency[neighbor].add(target)
                    adjacency[target].add(neighbor)
        del adjacency[vertex]
    return lower


def measure_graph(graph: nx.Graph) -> dict:
    if graph.number_of_nodes():
        fill, _ = treewidth_min_fill_in(graph)
        degree, _ = treewidth_min_degree(graph)
        lower = minor_min_width(graph)
    else:
        # Explicit convention for empty/ground queries; NetworkX returns -1.
        fill = degree = lower = 0
    upper = min(fill, degree)
    if lower > upper:
        raise AssertionError(f"invalid treewidth bounds {lower} > {upper}")
    return {"nodes": graph.number_of_nodes(), "edges": graph.number_of_edges(),
            "components": nx.number_connected_components(graph),
            "min_fill_upper": fill, "min_degree_upper": degree,
            "treewidth_lower": lower, "treewidth_upper": upper, "exact": lower == upper}


class Sources:
    def __init__(self):
        self.cache: dict[Path, list[SList]] = {}
        self.hashes: dict[Path, str] = {}

    def read(self, path: Path) -> list[SList]:
        path = path.resolve()
        if path not in self.cache:
            data = path.read_bytes()
            self.hashes[path] = hashlib.sha256(data).hexdigest()
            self.cache[path] = list(parse_program(data.decode("utf-8"), str(path)))
        return self.cache[path]

    def walk(self, path: Path, active: tuple[Path, ...] = ()) -> Iterator[tuple[Path, SList]]:
        path = path.resolve()
        if path in active:
            raise SourceError("include cycle: " + " -> ".join(map(str, (*active, path))))
        for form in self.read(path):
            if form.items and form.items[0] == "include":
                if len(form.items) != 2:
                    raise SourceError(f"{path}:{form.line}: invalid include")
                include = path.parent / string_value(form.items[1])
                yield from self.walk(include, (*active, path))
            else:
                yield path, form


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def analyze_suite(name: str, paths: list[Path], sources: Sources, model: str,
                  measurements: dict, unique: dict) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    locations: dict[tuple, str] = {}
    files: set[Path] = set()
    counts: Counter = Counter()
    for path in paths:
        env = Environment()
        for source, form in sources.walk(path):
            files.add(source)
            try:
                env.declare(form)
                queries = extract_queries(form)
                for query in queries:
                    lowered = QueryBuilder(env).build(query)
                    qid = lowered["query_id"]
                    location = (source, form.offset, query.direction)
                    if location in locations:
                        if locations[location] != qid:
                            raise SourceError("the same included rule has different binding contexts")
                        continue
                    locations[location] = qid
                    if qid not in measurements:
                        measurements[qid] = measure_graph(query_graph(lowered, model))
                    unique.setdefault((name, qid), {"suite": name, **lowered, **measurements[qid],
                                                  "source": display_path(source), "line": form.line})
                    counts[query.kind] += 1
                    rows.append({"suite": name, "source": display_path(source), "line": form.line,
                                 "offset": form.offset, "kind": query.kind, "direction": query.direction,
                                 "name": query.name, "ruleset": query.ruleset, "query_id": qid,
                                 "variables": lowered["variables"], "atoms": len(lowered["atoms"]),
                                 "max_hyperedge_size": max(map(len, lowered["hyperedges"]), default=0),
                                 "ignored_body_facts": len(lowered["ignored_body_facts"]),
                                 "ignored_when_facts": lowered["ignored_when_facts"],
                                 "ignored_primitive_calls": lowered["ignored_primitive_calls"],
                                 **measurements[qid]})
            except (SourceError, IndexError) as exc:
                raise SourceError(f"{source}:{form.line}: {exc}") from exc
    inventory = {"suite": name, "root_files": len(paths), "source_files": len(files),
                 "files_with_queries": len({r["source"] for r in rows}),
                 "directed_queries_by_kind": dict(counts), "occurrences": len(rows),
                 "unique_queries": len({r["query_id"] for r in rows}),
                 "sources": [{"path": display_path(p), "sha256": sources.hashes[p]} for p in sorted(files)]}
    return rows, inventory


def percentile(values: list[int], percent: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent / 100
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def summarize(rows: list[dict]) -> dict:
    values = [r["treewidth_upper"] for r in rows]
    if not values:
        return {"count": 0, "histogram": {}}
    return {"count": len(values), "min": min(values), "mean": statistics.mean(values),
            "stddev_population": statistics.pstdev(values), "median": statistics.median(values),
            "p90": percentile(values, 90), "p95": percentile(values, 95),
            "p99": percentile(values, 99), "max": max(values),
            "width_at_most_2_pct": 100 * sum(x <= 2 for x in values) / len(values),
            "certified_exact": sum(r["exact"] for r in rows),
            "certified_exact_pct": 100 * sum(r["exact"] for r in rows) / len(values),
            "max_bound_gap": max(r["treewidth_upper"] - r["treewidth_lower"] for r in rows),
            "histogram": dict(sorted(Counter(values).items()))}


def csv_write(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as stream:
        if rows:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def plot_distribution(summary: dict, output: Path, model: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator, PercentFormatter

    suites = list(summary)
    fig, axes = plt.subplots(2, len(suites), figsize=(4.4 * len(suites), 6.8), squeeze=False,
                             sharex="col", sharey=True, layout="constrained")
    colors = ["#2878A8", "#D17A22", "#3A8962"]
    for row, weighting in enumerate(("unique_queries", "occurrences")):
        for col, suite in enumerate(suites):
            ax = axes[row, col]
            stats = summary[suite][weighting]
            maximum = max((s["max"] for s in summary[suite].values() if s["count"]), default=0)
            hist = stats["histogram"]
            counts = [hist.get(x, 0) for x in range(maximum + 1)]
            fractions = [100 * n / stats["count"] if stats["count"] else 0 for n in counts]
            ax.bar(range(maximum + 1), fractions, color=colors[col % len(colors)], width=0.82)
            ax.set_title(f"{suite} · n = {stats['count']:,}")
            if stats["count"]:
                ax.text(0.97, 0.95, f"mean {stats['mean']:.2f} · median {stats['median']:g}\n"
                        f"95th percentile {stats['p95']:g} · max {stats['max']}",
                        transform=ax.transAxes, ha="right", va="top", fontsize=9)
            ax.yaxis.set_major_formatter(PercentFormatter(100))
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
            ax.grid(axis="y", alpha=0.2)
            ax.set_axisbelow(True)
            ax.spines[["top", "right"]].set_visible(False)
            if col == 0:
                label = "Distinct query bodies" if row == 0 else "All source occurrences"
                ax.set_ylabel(f"{label}\nShare of queries")
            if row == 1:
                ax.set_xlabel("Treewidth upper bound")
    fig.suptitle(f"Egglog query treewidth · {model} graph\n"
                 "Relational hyperedges only; conditions and built-in predicates excluded", fontsize=13)
    for extension in ("png", "svg", "pdf"):
        fig.savefig(output / f"distribution.{extension}", dpi=180)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append", metavar="NAME=PATH",
                        help="Analyze a file/directory; repeat for multiple suites. Defaults to luminal, eggcc, hardboiled.")
    parser.add_argument("--graph", choices=("primal", "incidence"), default="primal")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "treewidth")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args(argv)
    specifications = args.suite or [f"{name}={ROOT / 'benchmarks' / name}" for name in DEFAULT_SUITES]
    suites: dict[str, list[Path]] = {}
    for spec in specifications:
        if "=" not in spec:
            parser.error("--suite must be NAME=PATH")
        name, path_string = spec.split("=", 1)
        path = Path(path_string).resolve()
        if not name or name in suites:
            parser.error(f"empty or duplicate suite name: {name!r}")
        paths = sorted(path.rglob("*.egg")) if path.is_dir() else [path]
        if not paths or any(not p.is_file() for p in paths):
            parser.error(f"no egglog input files at {path}")
        suites[name] = paths

    sources, measurements, unique = Sources(), {}, {}
    all_rows, inventories = [], []
    try:
        for name, paths in suites.items():
            rows, inventory = analyze_suite(name, paths, sources, args.graph, measurements, unique)
            all_rows.extend(rows)
            inventories.append(inventory)
            print(f"{name}: {len(rows):,} query occurrences, {inventory['unique_queries']:,} distinct bodies",
                  file=sys.stderr)
    except (OSError, SourceError) as exc:
        parser.exit(1, f"error: {exc}\n")

    summary = {name: {
        "unique_queries": summarize([r for (suite, _), r in unique.items() if suite == name]),
        "occurrences": summarize([r for r in all_rows if r["suite"] == name]),
    } for name in suites}
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    csv_write(output / "queries.csv", all_rows)
    flat_summary = [{"suite": suite, "weighting": weighting, **{k: v for k, v in stats.items() if k != "histogram"}}
                    for suite, groups in summary.items() for weighting, stats in groups.items()]
    # Empty suites still have the same CSV schema as nonempty suites.
    fields = list(dict.fromkeys(k for row in flat_summary for k in row))
    csv_write(output / "summary.csv", [{k: r.get(k) for k in fields} for r in flat_summary])
    distribution = [{"suite": suite, "weighting": weighting, "treewidth_upper": width,
                     "count": count, "percent": 100 * count / stats["count"]}
                    for suite, groups in summary.items() for weighting, stats in groups.items()
                    for width, count in stats["histogram"].items()]
    csv_write(output / "distribution.csv", distribution)
    with (output / "unique_queries.jsonl").open("w") as stream:
        for row in unique.values():
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    report = {"graph_model": args.graph, "representation": "variables are vertices; stored relation/function/constructor atoms are hyperedges",
              "conditions": "rewrite :when clauses and primitive-root facts excluded; built-in calls contribute no hyperedges; equality aliases resolved",
              "bounds": "min(NetworkX min-fill, NetworkX min-degree); minor-min-width lower bound",
              "empty_graph_width": 0, "include_policy": "relative to including file; each source location counted once per suite",
              "deduplication": "flattened atoms in source traversal order, after equality/FD canonicalization and alpha-renaming; RHS ignored",
              "versions": {"python": platform.python_version(), "networkx": nx.__version__},
              "analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "suites": inventories, "statistics": summary}
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    if not args.no_plot:
        plot_distribution(summary, output, args.graph)
    print("\nsuite       weighting          n     mean   median  p95   max   exact")
    for row in flat_summary:
        if row["count"]:
            print(f"{row['suite']:<11} {row['weighting']:<17} {row['count']:>6}  {row['mean']:>6.2f}"
                  f"  {row['median']:>6g}  {row['p95']:>4g}  {row['max']:>4}  {row['certified_exact_pct']:>5.1f}%")
    print(f"\nResults: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
