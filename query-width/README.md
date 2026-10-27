# Query-width measurements

This directory is a standalone project for the egglog treewidth and JOB/LSQB
hypertree-width measurements. It contains its own code, dependency lockfile,
tests, input snapshots, plots, and statistics. Copy the entire directory to use
it independently of the surrounding repository. All default inputs and outputs
are resolved within this directory.

## Run

Requires Python 3.11 or later and [uv](https://docs.astral.sh/uv/).
Run these commands inside this directory:

```sh
uv sync --locked
uv run --locked python scripts/fetch-query-width-benchmarks.py --verify-only
uv run --locked python -m query_width.treewidth
uv run --locked python -m query_width.hypertreewidth
uv run --locked python -m unittest discover -s tests -v
```

The local `pyproject.toml`, `uv.lock`, and `.venv/` keep dependencies isolated.
After dependencies are installed, the bundled inputs suffice for offline runs.
Negative atoms, conditions, and built-in predicates are excluded under the
measurement definitions below; positive optional SQL atoms are retained.

## Contents

- `query_width/`: parsers, graph bounds, certificate verifier, and plotting code.
- `benchmarks/`: the 82 egglog source files used in the measurements, 113 JOB
  queries, nine LSQB queries, schemas, and source manifests. The egglog snapshots
  were copied from the original repository inputs; `egglog-manifest.json` records
  their paths and checksums.
- `scripts/`: restore or verify the pinned JOB and LSQB downloads.
- `tests/`: parser, graph, certificate, and benchmark checks.
- `results/treewidth/`: egglog query results and distributions.
- `results/hypertree-width/`: SQL query results, certificates, and distributions.

## Static query treewidth

`query_width/treewidth.py` parses egglog source and measures the treewidth of the
relational query hypergraph for each `rule`, `rewrite`, and direction of a
`birewrite`. It requires no compiled egglog binary or benchmark execution.

```sh
uv run --locked python -m query_width.treewidth
uv run --locked python -m query_width.treewidth --suite custom=path/to/program.egg --out /tmp/treewidth
uv run --locked python -m unittest discover -s tests -p test_treewidth.py -v
```

The default inputs are all `.egg` files under `benchmarks/{luminal,eggcc,hardboiled}`.
Repeat `--suite NAME=PATH` to analyze other files or directories. Includes resolve
relative to the including file; each physical source location counts once per suite.
Thus luminal's shared header counts once, while copies embedded in separate eggcc
or hardboiled files count separately. Malformed sources and missing/cyclic includes
fail the run rather than silently dropping queries.

The hypergraph has one vertex per query variable and one hyperedge per stored
relation, constructor, or function atom. Nested calls get intermediate result
variables: `(= z (F x (G y)))` becomes `G(y, t), F(x, t, z)`. Function and constructor
results are columns; declared relations have only their logical input columns.
Literal values, global bindings, and Unit results are fixed values, so they are
omitted from hyperedges. Nullary constructors still produce a result variable.
Equality aliases and repeated calls with equal inputs are merged to a fixed point;
the implementation follows egglog equality/functional-dependency normalization.

Only the query side is analyzed; actions and rewrite right-hand sides contribute
no atoms. Rewrite `:when` clauses are ignored in full. In explicit rule bodies,
primitive predicates such as `!=` and `<`, and equalities binding only primitive
computations, are ignored in full, including lookups nested in those conditions.
Primitive calls inside a retained relational pattern contribute no hyperedge;
their result can still occupy a column in a stored atom. Equality bindings between
stored calls, variables, and literals resolve aliases without adding hyperedges.
The analysis uses source declarations to distinguish stored atoms from primitives;
it assumes valid egglog and does not perform type checking or predicate evaluation.

The default metric is **treewidth of the hypergraph's primal graph**: variables
sharing a hyperedge form a clique. A single hyperedge with `k` variables therefore
has width `k-1`. Generalized/fractional hypertreewidth are different metrics.
`--graph incidence` instead measures the bipartite variable–hyperedge graph.
The reported upper bound is the smaller of NetworkX's
[min-fill](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.approximation.treewidth.treewidth_min_fill_in.html)
and [min-degree](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.approximation.treewidth.treewidth_min_degree.html)
elimination bounds. A minor-min-width lower bound repeatedly records the minimum
degree and contracts an incident edge; matching bounds certify the exact width.
Empty and ground queries have width zero by convention. These are static logical
query measurements, before engine-specific delta queries or decomposition.

Results are written to `results/treewidth/` (override with `--out`):

- `distribution.{png,svg,pdf}`: distributions for distinct bodies and all source occurrences.
- `queries.csv`: one row per directed query, with location, ruleset/name, counts of
  ignored conditions, both heuristic bounds, lower bound, and exactness flag.
- `unique_queries.jsonl`: the flattened atoms and explicit variable-ID hyperedges
  for each distinct query, with a representative source location and its bounds.
- `summary.{json,csv}` and `distribution.csv`: counts, mean, population standard
  deviation, median, percentiles, extrema, exactness, and histogram data. JSON also
  records dependency versions and source/analyzer SHA-256 hashes.

Distinct bodies are deduplicated within each suite after flattening, alias merging,
and renaming variables by first occurrence. Names, rulesets, actions, and ignored
conditions do not affect identity. Atom order and relation symbols are preserved;
this is syntactic deduplication, not query equivalence or graph isomorphism.

For the current 82 source files, the default analysis produces 31,521 directed
query occurrences. Statistics below weight each distinct body once:

| Suite | Occurrences | Distinct bodies | Mean | Median | P95 | Max | Certified exact |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| luminal | 491 | 475 | 5.73 | 4 | 25 | 27 | 473 / 475 |
| eggcc | 21,664 | 611 | 2.59 | 3 | 4 | 5 | 607 / 611 |
| hardboiled | 9,366 | 193 | 2.73 | 3 | 4 | 9 | 193 / 193 |

Luminal's width-27 queries contain a `cublaslt` hyperedge with 28 variables.
Hardboiled's maximum is a ten-variable `matrix-multiplication-int/uint-cont`
hyperedge. The six distinct queries without matching bounds have gaps of at most
three (luminal) and one (eggcc). Occurrence-weighted means are 5.66, 2.55, and 2.68,
respectively; full distributions and additional statistics are in the result files.

## JOB and LSQB hypertree width

The source snapshots in `benchmarks/job/` and `benchmarks/lsqb/` contain all 113
[JOB SQL queries](https://github.com/gregrahn/join-order-benchmark) and all nine
[LSQB SQL queries](https://github.com/ldbc/lsqb/tree/main/sql), plus their schemas.
LSQB's Cypher queries and SQL view definitions are also retained for reference.
Each `manifest.json` records the repository, pinned commit, original URL, and
SHA-256/Git blob checksums for every file. No database contents are needed.

```sh
# Verify the snapshots, or restore missing files from their pinned URLs.
python scripts/fetch-query-width-benchmarks.py --verify-only
python scripts/fetch-query-width-benchmarks.py

# Analyze both suites and generate the plots and data.
uv run --locked python -m query_width.hypertreewidth
uv run --locked python -m unittest discover -s tests -p test_hypertreewidth.py -v
```

The snapshots are pinned to JOB `a39603662e023e449cb2121997a5034df9e02ebf`
and LSQB `3e455f6255cbbf017f0bcb0144b50d3f1b582f58`. The fetch script refuses
to overwrite locally modified sources. The analyzer verifies all source hashes
before measuring them. `--suite job` or `--suite lsqb` selects one suite;
`--out PATH` changes the default output directory, `results/hypertree-width/`.

SQLGlot parses each SQL query into an AST. Each table or logical-view alias is one
hyperedge, and each schema column starts as a variable. Direct conjunctive
column-to-column equality joins merge variables, including transitive and
redundant equalities. Self-join aliases remain separate atoms. All schema columns
are retained, including relation-local columns. Literal comparisons, inequalities,
OR predicates, null checks, and aggregate/output expressions contribute no edges.
View definitions supply output column names; the views remain logical relations
instead of being expanded into their UNION implementations. These results use
LSQB's SQL representation, with its merged foreign keys, rather than a Cypher
operator plan. The supported SQL fragment is a single SELECT over named relations
with explicit/implicit equality joins; unsupported scopes, USING/NATURAL joins,
and unresolved columns fail explicitly.

The metric is **standard hypertree width**: the maximum number of original
hyperedges guarding a bag in a hypertree decomposition. The verifier checks
hyperedge containment, connected variable occurrences, guard coverage of every
bag, and the standard *special condition* concerning guard variables in descendant
bags. See the [original definition](https://arxiv.org/abs/cs/9812022).
A single relation of any positive arity has hypertree width one.

For acyclic hypergraphs, the analyzer constructs and validates a join tree, which
certifies width one. For cyclic hypergraphs it searches guarded separators and
returns a validated standard hypertree decomposition. Non-acyclicity proves a
lower bound of two; a projected primal-graph treewidth/rank bound can improve it.
Search failures never establish lower bounds. `--max-width` (default 3) and
`--timeout` (default 10 seconds per cyclic hypergraph) limit the search; on failure
the analyzer returns a validated conservative one-bag bound and marks any gap.
For these benchmarks all upper and lower bounds match. Tests independently check
acyclicity using GYO reduction, including every downloaded query.

The analysis measures `positive_relations`: negative atoms and all of their ON
constraints are excluded. LSQB q8's `cht2` and q9's `pkp3` are recognized as negative
lookups by their LEFT JOIN followed by a conjunctive IS NULL test on an equality-join
column. A matched row cannot have NULL in that column; a NULL test on an arbitrary
nullable payload is not sufficient to identify a negative atom. Positive optional
atoms are retained, including both optional lookups in q7. These are structural
widths of the retained relation patterns; a decomposition alone does not describe
optional-join execution.

All reported widths below are exact; each SQL file receives equal weight:

| Suite / positive relation pattern | Queries | Width 1 | Width 2 | Mean | Median | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| JOB | 113 | 113 | 0 | 1.00 | 1 | 1 |
| LSQB | 9 | 7 | 2 | 1.22 | 1 | 2 |

LSQB q2 and q3 have width two; q1 and q4–q9 have width one. JOB's many written
join equalities collapse into acyclic hypergraphs when equal columns share a vertex.

`queries.csv` contains per-query bounds, graph sizes, and excluded negative-atom counts;
`hypergraphs.jsonl` additionally records every column equivalence class, relation
hyperedge, excluded relation, ignored predicate, and checked decomposition certificate.
`summary.{csv,json}` contains statistics and provenance, and
`distribution.{png,svg,pdf}` plots the positive-pattern distributions for JOB and LSQB.

