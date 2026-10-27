"""Measure standard hypertree width of the pinned JOB and LSQB SQL queries.

Variables are column equivalence classes; each table/view alias is a hyperedge.
Filters, aggregates, and negative relation atoms are ignored. Positive optional
atoms are retained, so this measures the positive relation-pattern hypergraph.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import platform
import re
import statistics
import time
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

import networkx as nx
import sqlglot
from sqlglot import exp

from query_width.treewidth import ROOT, csv_write, minor_min_width, percentile


def verify_sources(suite: str) -> dict:
    root = ROOT / "benchmarks" / suite
    manifest = json.loads((root / "manifest.json").read_text())
    for entry in manifest["files"]:
        path = root / entry["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"missing/modified source: {path}; run scripts/fetch-query-width-benchmarks.py")
    return manifest


def load_schema(paths: list[Path]) -> dict[str, tuple[str, ...]]:
    """Read base table columns and the public output columns of logical views."""
    schema = {}
    for path in paths:
        for statement in sqlglot.parse(path.read_text(), read="postgres"):
            if not isinstance(statement, exp.Create):
                raise ValueError(f"{path}: expected CREATE TABLE/VIEW")
            if statement.kind == "TABLE" and isinstance(statement.this, exp.Schema):
                name = statement.this.this.name.lower()
                columns = tuple(c.name.lower() for c in statement.this.expressions if isinstance(c, exp.ColumnDef))
            elif statement.kind == "VIEW":
                name = statement.this.name.lower()
                columns = tuple(c.lower() for c in statement.expression.named_selects)
            else:
                raise ValueError(f"{path}: unsupported schema declaration")
            if not columns or "*" in columns or len(set(columns)) != len(columns):
                raise ValueError(f"{path}: unknown or duplicate columns in {name}")
            schema[name] = columns
    return schema


def conjuncts(node):
    if node is None:
        return
    if isinstance(node, (exp.Where, exp.Paren)):
        yield from conjuncts(node.this)
    elif isinstance(node, exp.And):
        yield from conjuncts(node.this)
        yield from conjuncts(node.expression)
    else:
        yield node


def sql_hypergraph(text: str, schema: dict[str, tuple[str, ...]], mandatory_only: bool = False) -> dict:
    statements = sqlglot.parse(text, read="postgres")
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("expected one SELECT query")
    query = statements[0]
    if len(list(query.find_all(exp.Select))) != 1 or query.args.get("with_"):
        raise ValueError("subqueries and CTEs must be analyzed separately")
    from_ = query.args.get("from_")
    if from_ is None:
        raise ValueError("query has no FROM relation")
    joins = query.args.get("joins", [])
    entries = [(from_.this, "required", None)]
    for join in joins:
        if join.args.get("using") or join.args.get("method") or join.side not in ("", "LEFT"):
            raise ValueError("only explicit equality joins, cross joins and LEFT JOINs are supported")
        if join.kind not in ("", "INNER", "OUTER", "CROSS"):
            raise ValueError(f"unsupported join kind: {join.kind}")
        entries.append((join.this, "left_join" if join.side == "LEFT" else "required", join.args.get("on")))
    aliases, relations = {}, []
    for table, role, _ in entries:
        if not isinstance(table, exp.Table):
            raise ValueError("FROM/JOIN source must be a named table or logical view")
        alias, name = table.alias_or_name.lower(), table.name.lower()
        if name not in schema or alias in aliases:
            raise ValueError(f"unknown table or duplicate alias: {table.sql()}")
        aliases[alias] = name
        relations.append({"alias": alias, "table": name, "role": role})

    def column(node: exp.Column) -> str:
        name, alias = node.name.lower(), node.table.lower()
        if not alias:
            matches = [a for a, table in aliases.items() if name in schema[table]]
            if len(matches) != 1:
                raise ValueError(f"unresolved/ambiguous column: {node.sql()}")
            alias = matches[0]
        if alias not in aliases or name not in schema[aliases[alias]]:
            raise ValueError(f"unknown column: {node.sql()}")
        return f"{alias}.{name}"

    # Validate references even in ignored filters and output expressions.
    for node in query.find_all(exp.Column):
        column(node)
    where = list(conjuncts(query.args.get("where")))
    null_columns = {column(c.this) for c in where
                    if isinstance(c, exp.Is) and isinstance(c.this, exp.Column)
                    and isinstance(c.expression, exp.Null)}
    for relation, (_, _, on) in zip(relations, entries):
        if relation["role"] != "left_join":
            continue
        # An equality-join column cannot be NULL in a matched row. Testing it
        # for NULL selects only unmatched rows: the alias is a negative atom.
        # A NULL test on an arbitrary nullable payload is not an anti join.
        join_columns = {column(side) for condition in conjuncts(on)
                        if isinstance(condition, exp.EQ)
                        and isinstance(condition.this, exp.Column)
                        and isinstance(condition.expression, exp.Column)
                        for side in (condition.this, condition.expression)}
        if any(c.startswith(relation["alias"] + ".") for c in null_columns & join_columns):
            relation["role"] = "negative"
    included = {r["alias"] for r in relations if r["role"] != "negative"
                and (not mandatory_only or r["role"] == "required")}
    parent = {f"{alias}.{col}": f"{alias}.{col}" for alias in included for col in schema[aliases[alias]]}

    def find(value):
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    conditions = [(c, True) for c in where]
    for relation, (_, _, on) in zip(relations, entries):
        # None of an excluded atom's ON constraints may merge surviving columns.
        conditions.extend((c, relation["alias"] in included) for c in conjuncts(on))
    equalities, ignored = [], []
    for condition, active in conditions:
        if active and isinstance(condition, exp.EQ) and isinstance(condition.this, exp.Column) and isinstance(condition.expression, exp.Column):
            a, b = column(condition.this), column(condition.expression)
            if a in parent and b in parent:
                x, y = find(a), find(b)
                parent[max(x, y)] = min(x, y)
                equalities.append([a, b])
                continue
        ignored.append(condition.sql(dialect="postgres"))

    classes = defaultdict(list)
    for col in sorted(parent):
        classes[find(col)].append(col)
    variables = list(classes.values())
    vertex = {col: i for i, group in enumerate(variables) for col in group}
    edges = [{**r, "vertices": sorted({vertex[f"{r['alias']}.{c}"] for c in schema[r["table"]]})}
             for r in relations if r["alias"] in included]
    return {"variables": variables, "edges": edges, "equalities": equalities,
            "ignored_predicates": ignored, "relation_roles": relations,
            "excluded_relations": [r for r in relations if r["alias"] not in included],
            "pattern": "mandatory_only" if mandatory_only else "positive_relations"}


def verify_decomposition(edges: list[frozenset[int]], root: dict) -> int:
    """Independently check all four standard HD axioms, including specialness."""
    nodes, links = [], []

    def visit(node, parent=None):
        index = len(nodes)
        nodes.append(node)
        if parent is not None:
            links.append((parent, index))
        bag, guards = set(node["bag"]), node["guards"]
        if len(set(guards)) != len(guards) or any(g < 0 or g >= len(edges) for g in guards):
            raise ValueError("invalid guard edge IDs")
        cover = set().union(*(edges[g] for g in guards))
        if not bag <= cover:
            raise ValueError("bag is not covered by its guards")
        subtree = set(bag)
        for child in node["children"]:
            subtree.update(visit(child, index))
        if not (cover & subtree) <= bag:
            raise ValueError("hypertree special condition violated")
        return subtree

    used = visit(root)
    vertices = set().union(*edges)
    if used != vertices:
        raise ValueError("decomposition has missing or extra vertices")
    if any(not any(edge <= set(node["bag"]) for node in nodes) for edge in edges):
        raise ValueError("a hyperedge is not contained in any bag")
    tree = nx.Graph()
    tree.add_nodes_from(range(len(nodes)))
    tree.add_edges_from(links)
    for vertex in vertices:
        containing = [i for i, node in enumerate(nodes) if vertex in node["bag"]]
        if not nx.is_connected(tree.subgraph(containing)):
            raise ValueError("running intersection violated")
    return max((len(node["guards"]) for node in nodes), default=0)


def join_tree(edges: list[frozenset[int]]) -> dict | None:
    """A maximum-intersection spanning tree is a join tree iff one exists."""
    graph = nx.Graph()
    graph.add_nodes_from(range(len(edges)))
    for i, j in itertools.combinations(range(len(edges)), 2):
        graph.add_edge(i, j, weight=len(edges[i] & edges[j]))
    tree = nx.maximum_spanning_tree(graph)

    def node(index, parent=None):
        return {"bag": sorted(edges[index]), "guards": [index],
                "children": [node(child, index) for child in sorted(tree[index]) if child != parent]}

    root = node(0)
    try:
        verify_decomposition(edges, root)
    except ValueError:
        return None
    return root


def bits(mask: int):
    while mask:
        bit = mask & -mask
        yield bit.bit_length() - 1
        mask ^= bit


class SearchTimeout(Exception):
    pass


def guarded_decomposition(edges: list[frozenset[int]], width: int, deadline: float) -> dict | None:
    """Search normal-form HDs by guarded separators of at most ``width`` edges.

    A state has an unprocessed connected component C and its outside neighbors B.
    Guards must cover B. Its bag is union(guards) intersect (C union B), which
    enforces specialness because all descendant bags stay inside C union B.
    Each child is a component of C minus the bag. Only successful, independently
    validated certificates are used as upper bounds; search failure is not used
    as a numerical lower bound.
    """
    vertices = set().union(*edges)
    edge_masks = [sum(1 << v for v in e) for e in edges]
    adjacency = {v: 0 for v in vertices}
    for edge, mask in zip(edges, edge_masks):
        for v in edge:
            adjacency[v] |= mask & ~(1 << v)

    def neighbors(mask):
        result = 0
        for v in bits(mask):
            result |= adjacency[v]
        return result

    def components(mask):
        out = []
        while mask:
            part, frontier = 0, mask & -mask
            while frontier:
                part |= frontier
                frontier = neighbors(frontier) & mask & ~part
            out.append(part)
            mask &= ~part
        return out

    candidates = {}
    for count in range(1, min(width, len(edges)) + 1):
        for guards in itertools.combinations(range(len(edges)), count):
            if time.monotonic() > deadline:
                raise SearchTimeout
            cover = 0
            for edge in guards:
                cover |= edge_masks[edge]
            candidates.setdefault(cover, guards)

    @lru_cache(maxsize=None)
    def solve(component, boundary):
        if time.monotonic() > deadline:
            raise SearchTimeout
        scope = component | boundary
        choices = [(cover & scope, guards) for cover, guards in candidates.items()
                   if cover & boundary == boundary and cover & component]
        # Prefer separators that remove more vertices; deterministic ties.
        choices.sort(key=lambda pair: (-(pair[0] & component).bit_count(), len(pair[1]), pair[1]))
        tried_bags = set()
        for bag, guards in choices:
            if bag in tried_bags:
                continue
            tried_bags.add(bag)
            children = []
            for child_component in components(component & ~bag):
                boundary_child = neighbors(child_component) & ~child_component
                child = solve(child_component, boundary_child)
                if child is None:
                    break
                children.append(child)
            else:
                return {"bag": list(bits(bag)), "guards": list(guards), "children": children}
        return None

    children = []
    for component in components(sum(1 << v for v in vertices)):
        child = solve(component, 0)
        if child is None:
            return None
        children.append(child)
    return {"bag": [], "guards": [], "children": children}


def measure_hypergraph(raw_edges: list[list[int]], max_width: int = 3, timeout: float = 10) -> dict:
    edges = [frozenset(edge) for edge in raw_edges]
    vertices = set().union(*edges)
    if not vertices:
        return {"hypertree_lower": 0, "hypertree_upper": 0, "exact": True,
                "method": "empty", "decomposition": {"bag": [], "guards": [], "children": []}}
    acyclic = join_tree(edges)
    if acyclic is not None:
        return {"hypertree_lower": 1, "hypertree_upper": 1, "exact": True,
                "method": "join_tree", "decomposition": acyclic}

    # Non-alpha-acyclic hypergraphs have hypertree width at least two. A vertex
    # projection gives another safe lower bound: tw(primal) + 1 <= rank * hw.
    degree = Counter(v for edge in edges for v in edge)
    shared = {v for v, n in degree.items() if n > 1}
    projection = [edge & shared for edge in edges]
    graph = nx.Graph()
    graph.add_nodes_from(shared)
    for edge in projection:
        graph.add_edges_from(itertools.combinations(sorted(edge), 2))
    rank = max(map(len, projection), default=0)
    lower = max(2, math.ceil((minor_min_width(graph) + 1) / rank) if rank else 0)
    fallback = {"bag": sorted(vertices), "guards": list(range(len(edges))), "children": []}
    root, method = fallback, "one_bag_fallback"
    deadline = time.monotonic() + timeout
    for width in range(lower, min(max_width, len(edges)) + 1):
        try:
            candidate = guarded_decomposition(edges, width, deadline)
        except SearchTimeout:
            method = "timeout_one_bag_fallback"
            break
        if candidate is not None:
            root, method = candidate, "guarded_separator"
            break
    upper = verify_decomposition(edges, root)
    if lower > upper:
        raise AssertionError(f"invalid hypertree width bounds: {lower} > {upper}")
    return {"hypertree_lower": lower, "hypertree_upper": upper, "exact": lower == upper,
            "method": method, "decomposition": root}


def summarize(rows: list[dict]) -> dict:
    values = [r["hypertree_upper"] for r in rows]
    return {"count": len(values), "min": min(values), "mean": statistics.mean(values),
            "stddev_population": statistics.pstdev(values), "median": statistics.median(values),
            "p90": percentile(values, 90), "p95": percentile(values, 95), "max": max(values),
            "exact_count": sum(r["exact"] for r in rows), "histogram": dict(sorted(Counter(values).items()))}


def plot_results(rows: list[dict], output: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator, PercentFormatter

    suites = list(dict.fromkeys(r["suite"] for r in rows))
    fig, axes = plt.subplots(1, len(suites), figsize=(5 * len(suites), 4), squeeze=False, layout="constrained")
    maximum = max(r["hypertree_upper"] for r in rows)
    colors = ["#2878A8", "#D17A22"]
    for col, suite in enumerate(suites):
        selected = [r for r in rows if r["suite"] == suite]
        hist = Counter(r["hypertree_upper"] for r in selected)
        ax = axes[0, col]
        xs = list(range(1, maximum + 1))
        counts = [hist[x] for x in xs]
        bars = ax.bar(xs, [100 * n / len(selected) for n in counts],
                      width=0.55, color=colors[col % len(colors)])
        ax.bar_label(bars, labels=[str(n) if n else "" for n in counts], padding=4)
        ax.set_title(f"{suite.upper()} · {len(selected)} queries")
        ax.set_xlabel("Hypertree width" if all(r['exact'] for r in rows) else "Hypertree width upper bound")
        ax.set_ylabel("Share of queries")
        ax.set_ylim(0, 112)
        ax.set_xticks(xs)
        ax.yaxis.set_major_formatter(PercentFormatter(100))
        ax.yaxis.set_major_locator(MaxNLocator(6))
        ax.grid(axis="y", alpha=0.2)
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Standard hypertree width of SQL relation patterns\n"
                 "Negative atoms and filters excluded; positive optional atoms retained", fontsize=12)
    for extension in ("png", "svg", "pdf"):
        fig.savefig(output / f"distribution.{extension}", dpi=180)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("job", "lsqb"), action="append")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "hypertree-width")
    parser.add_argument("--max-width", type=int, default=3, help="largest guard count to search before returning a conservative bound")
    parser.add_argument("--timeout", type=float, default=10, help="seconds per cyclic hypergraph")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args(argv)
    if args.max_width < 1 or args.timeout <= 0:
        parser.error("--max-width and --timeout must be positive")
    rows, graphs, sources, cache = [], [], [], {}
    for suite in args.suite or ("job", "lsqb"):
        manifest = verify_sources(suite)
        sources.append(manifest)
        source_root = ROOT / "benchmarks" / suite
        sql_root = source_root if suite == "job" else source_root / "sql"
        schema_files = [sql_root / "schema.sql"] + ([sql_root / "views.sql"] if suite == "lsqb" else [])
        schema = load_schema(schema_files)
        pattern = r"\d+[a-z]\.sql" if suite == "job" else r"q\d+\.sql"
        paths = sorted((p for p in sql_root.glob("*.sql") if re.fullmatch(pattern, p.name)),
                       key=lambda p: (int(re.search(r"\d+", p.name).group()), p.name))
        if len(paths) != manifest["query_count"]:
            raise ValueError(f"{suite}: expected {manifest['query_count']} queries, found {len(paths)}")
        for path in paths:
            hypergraph = sql_hypergraph(path.read_text(), schema)
            key = tuple(tuple(e["vertices"]) for e in hypergraph["edges"])
            if key not in cache:
                cache[key] = measure_hypergraph([list(edge) for edge in key], args.max_width, args.timeout)
            result = cache[key]
            row = {"suite": suite, "query": path.stem, "source": str(path.relative_to(ROOT)),
                   "pattern": hypergraph["pattern"], "variables": len(hypergraph["variables"]),
                   "relations": len(hypergraph["edges"]), "max_arity": max(map(len, key), default=0),
                   "excluded_negative_atoms": sum(r["role"] == "negative" for r in hypergraph["excluded_relations"]),
                   **{k: v for k, v in result.items() if k != "decomposition"}}
            rows.append(row)
            graphs.append({**row, **hypergraph, "decomposition": result["decomposition"]})
        print(f"{suite}: {len(paths)} queries analyzed")
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    csv_write(output / "queries.csv", rows)
    stats = [{"suite": suite, "pattern": "positive_relations", **summarize([r for r in rows if r["suite"] == suite])}
             for suite in dict.fromkeys(r["suite"] for r in rows)]
    csv_write(output / "summary.csv", [{k: v for k, v in r.items() if k != "histogram"} for r in stats])
    with (output / "hypergraphs.jsonl").open("w") as stream:
        for graph in graphs:
            stream.write(json.dumps(graph, sort_keys=True) + "\n")
    manifest = {"metric": "standard hypertree width (special condition enforced)",
                "model": "SQL table/view aliases as hyperedges, full schema columns as vertices, direct conjunctive column equalities unified",
                "filters": "ignored, including inequalities, literal comparisons, OR, IS NULL; aggregate/head does not add an edge",
                "views": "logical relations with declared output columns; view implementations are not expanded",
                "outer_anti_joins": "positive optional atoms retained; negative atoms (LEFT JOIN with a conjunctive IS NULL test on an equality-join column) and all their ON constraints excluded",
                "bounds": "validated standard HD certificates; lower bounds from alpha-acyclicity and projected primal treewidth/rank",
                "search": {"max_width": args.max_width, "timeout_seconds": args.timeout},
                "versions": {"python": platform.python_version(), "networkx": nx.__version__, "sqlglot": sqlglot.__version__},
                "analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "treewidth_helper_sha256": hashlib.sha256(Path(__file__).with_name('treewidth.py').read_bytes()).hexdigest(),
                "sources": sources, "statistics": stats}
    (output / "summary.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if not args.no_plot:
        plot_results(rows, output)
    print("\nsuite  pattern          n   mean  median  max   exact")
    for r in stats:
        print(f"{r['suite']:<6} {r['pattern']:<15} {r['count']:>3}  {r['mean']:>5.2f}  {r['median']:>6g}  {r['max']:>3}  {r['exact_count']}/{r['count']}")
    print(f"\nResults: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
