# egglog benchmarks

Benchmark harness for the egglog system paper. It runs a matrix of
*variants* (a system at a git ref, plus flags) over *benchmarks*, stores one
JSON row per run, and turns the rows into the paper's figures.

## Setup

```sh
git submodule update --init egglog          # egglog source; any ref in it can be benchmarked
unzip -q benchmarks/pointer-analysis/pointer-analysis.zip -d benchmarks/pointer-analysis
uv sync                                     # python env for the harness (https://astral.sh/uv)
```

## Usage

```sh
uv run bench list                 # experiments, suites, systems
uv run bench list e2e             # the matrix of one experiment
uv run bench run e2e              # run it (appends results/e2e/<run-id>.jsonl)
uv run bench run e2e -b math 'hardboiled/conv1d_*' -v egglog -r 1 --tag quick
uv run bench summary e2e          # median times + geomean speedups (latest run)
uv run bench check e2e            # do all variants reach the same table sizes?
uv run bench plot e2e --out ../egglog-system-paper/figures
uv run bench build egglog v0.5.0  # build a ref by hand
```

summary/check/plot merge all runs of an experiment by default (the latest run wins per
configuration), so an experiment can be run in pieces; `--run latest` or `--run <id|tag>`
selects one run.

## Layout

| path | what |
|---|---|
| `benchmarks/<suite>/*.egg` | one benchmark per file, or a `suite.toml` naming one program and a glob of fact directories |
| `benchmarks/<suite>/<system>/` | ports of the suite to other systems (a system supports a benchmark iff its port exists) |
| `systems/<system>/build <ref>` | builds the system at a git ref, prints the build dir, writes `build.json` there |
| `systems/<system>/run <build-dir> <program> <fact-dir|-> <threads> <out-dir> [flags]` | runs one benchmark; writes `stdout`, `stderr`, `report.json`; exit 3 = unsupported |
| `experiments/<name>.toml` | variants, benchmarks, thread counts, reps, timeout, CPU pinning, plot kind, baseline |
| `results/<experiment>/<run-id>.jsonl` | one row per run (committed); `results/<experiment>/logs/` holds raw output (not committed) |
| `figures/` | output of `bench plot` (PDF + PNG + CSV) |
| `build/` | git worktrees and cargo targets, keyed by commit (not committed) |
| `harness/` | the `bench` CLI: `config.py` (layout), `runner.py`, `results.py`, `plot.py`, `cli.py` |

## Adding things

- **A benchmark**: drop a `.egg` file into a suite directory. Files a suite's programs `include`
  (like `benchmarks/luminal/luminal-header.egg`) are listed under `exclude` in its `suite.toml`.
- **An egglog commit to compare**: add a `[[variants]]` block with `ref = "<commit|tag|branch>"`.
- **An ablation**: a variant with `flags = [...]`. Flags a build does not know are reported as `unsupported`.
- **A local change to egglog** (e.g. a new ablation flag): a patch in `systems/egglog/patches/<name>.patch`,
  listed in the variant as `patches = ["<name>"]`. It is applied with `git apply --3way` on a pristine
  checkout of the ref; if upstream drifts so that it no longer applies, the build fails and says so,
  and the patch is regenerated with `git diff` against the new pin. Build directories are keyed by
  commit plus patch hash, so patched and unpatched builds coexist.
- **An experiment**: a new `experiments/<name>.toml`; `plot` is `bars` (grouped by benchmark, with a speedup figure if `baseline` is set) or `scaling` (speedup vs. thread count).
- **A baseline system**: `systems/<name>/build` and `systems/<name>/run` honoring the contract above, a `system.toml` with `port_dir = "<name>"`, and ports under `benchmarks/<suite>/<name>/`.
  - `egg`: a port is a Rust file `benchmarks/<suite>/egg/<name>.rs` with a `main` that takes `--report FILE`
    and prints e-nodes per constructor in `(print-size)` format; `build <ref>` compiles every port against
    that egg checkout (`build/egg/<commit>/ports`).
  - `oatlog`: a port is an egglog program `benchmarks/<suite>/oatlog/<name>.egg` in oatlog's subset of the
    language (a symlink to the original when it already is); `build <ref>` compiles it into a binary with
    `compile_egraph_strict` (and `--relaxed` selects `compile_egraph_relaxed`).

## Result row

```json
{"experiment": "e2e", "run_id": "20261009-004630", "suite": "math", "benchmark": "math",
 "variant": "egglog", "system": "egglog", "ref": "bench-ablation", "commit": "6795c2…", "flags": [],
 "threads": 1, "rep": 0, "status": "ok", "wall_s": 1.67, "user_s": 1.6, "sys_s": 0.05, "max_rss_kb": 1234,
 "report": {"run_s": 1.5, "search_apply_s": 1.3, "merge_s": 0.1, "rebuild_s": 0.1, "sizes": {"Add": 12345}},
 "log_dir": "results/e2e/logs/…", "host": {...}, "harness_commit": "a85aa6e", "timestamp": "…"}
```

`status` is `ok`, `fail`, `timeout`, `oom` or `unsupported`. `wall_s` is the
system's own process wall time as reported by the adapter (`harness_wall_s` is
measured around everything, including the adapter and cgroup setup); the other
`report.*` fields are the system's own numbers (for egglog, from `(print-stats)`
and `(print-size)` appended as a trailer file).

Every run gets `timeout` seconds and `memory_gb` GB (defaults 600 s and 32 GB,
per experiment or `--timeout`/`--memory-gb` on `bench run`). The memory cap is a
transient systemd user scope (`MemoryMax`, no swap), so an over-budget run is
OOM-killed and recorded as `oom`; without systemd it falls back to an
address-space rlimit.

## Systems

| system | how it is built | pinned ref in `experiments/engines.toml` | notes |
|---|---|---|---|
| `egglog` | host cargo, any ref of the submodule, optional patches | 593df10e, v0.5.0 | `-j`, `--no-decomp`, `--no-dynamic-order` (patch) |
| `egg` | host cargo; Rust port per benchmark | v0.11.0 | `SimpleScheduler`, no limits |
| `oatlog` | host cargo; port = egglog program compiled by the proc macro | 6b73396 (main) | strict mode; relaxed mode exceeds 32 GB |
| `hegg` | container (GHC 9.6); Haskell port | v0.5.0.0 | driver loop in the port (hegg's own caps iterations at 30 and bans rules); the port canonicalizes the e-matching database because hegg's leaves stale ids |
| `metatheory` | container (Julia 1.11); Julia port | v2.0.2 (latest release; no 3.x release) | `SimpleScheduler`, limits disabled, JIT warm-up excluded from `run_s` |
| `ggs` | container (SBCL 2.5); Common Lisp port of the `ggs/eqsat` system | 097e4f32 (main, no tags) | engine's own `run-rewrites` driver, no match limit; standalone executable |
| `ego` | container (OCaml 4.14); OCaml port | e87dae7a (main) | the 0.0.6 tag has an e-matching bug (one match per class); for fixed-iteration programs egg-style iterations are forced through the public runner (~15% extra), saturating programs use ego's own loop |
| `foresight` | container (JDK 21, Scala 3.4); Scala port | f3ea706d (main) | v0.1.0 predates the mutable e-graph; `ParallelMap.sequential` for 1 thread, `fixedThreadParallel(N)` for `--threads N` (variants `foresight-t4/t8/t16`); JVM default G1 GC in every variant |

Every port of `math-microbenchmark` reaches exactly the same e-graph as egglog
(1047896 e-nodes, 443832 e-classes); `bench check engines` verifies it on every run.
Container systems follow the contract in `systems/_container/README.md`.

## Measurement policy

The headline number is always `wall_s`: wall-clock time measured by our own code
(the harness, an adapter or a port we wrote) around the whole run, including parsing
and type checking where the engine does them. For JIT languages (Julia, JVM) the port
measures after a warm-up on a separate e-graph, so JIT compilation and VM startup are
excluded while e-graph construction, the iterations and the size count are included.
Times the engines report about themselves (egglog's `(print-stats)`, egg's iteration
times, Metatheory's report, ...) are stored in `report.run_s` and the like for
reference, never plotted.

## Cross-engine comparison

`experiments/engines.toml` compares engines on programs every engine can run *exactly*:
`math-microbenchmark` (pure rewrites, fixed iteration count: one iteration does the same
work in every engine) and the two saturating workloads of the foresight CC'26 paper,
`horner` (Horner's method, degrees 5 and 6 as in the paper plus larger ones) and
`matmul-assoc` (matrix-chain associativity, 20/40/80 as in the paper plus larger), both
generated by a `gen.py` in the suite from foresight's own example rules and run until a
whole iteration changes nothing. `bench check engines` verifies that every engine ends
with the same e-graph (and the ports also report their iteration counts). The paper's
extraction step is not benchmarked. Benchmarks named `base-<N>` are served by a single
port `base` per system that takes `--instance N`. The `math` suite stays egglog-only: it drives the rewrites with
`evals-to`/`MathU` relations, which egg can only express as an eager analysis (so its
iterations do more work than egglog's) and which uses `!=` on e-classes, which oatlog
does not support; both deviations change the workload by orders of magnitude.

## Math microbenchmark: 12 iterations

`experiments/math-microbenchmark-12.toml` compares oatlog, egglog at 1/4/8/16
threads, egglog 0.5.0, and ggs on `math-microbenchmark-12` (`run 12`). It uses
five repetitions, a 600-second timeout and a 32 GB memory cap per run, and plots
median process wall time with the same pinned engine versions as `engines`.

```sh
uv run bench run math-microbenchmark-12
uv run bench summary math-microbenchmark-12
uv run bench check math-microbenchmark-12
uv run bench plot math-microbenchmark-12
```

The figure is written to `figures/math-microbenchmark-12.pdf` and `.png`, with
summary data in `figures/math-microbenchmark-12.csv`.

## Query-only comparison with DuckDB and Umbra

`experiments/query.toml` (`kind = "query"`, driven by `harness/query.py`): every rule of every
luminal program, run once on the program's final state, in egglog and as SQL in DuckDB and
Umbra. Translation and timing rules are in `systems/egglog-query/system.toml` (`notes`).

```
uv run bench run query                    # build driver, export, translate, run all engines
uv run bench run query -b llama -v duckdb # one program, only DuckDB (plus egglog)
uv run bench summary query                # per program: summed optimize/execute, failures
uv run bench check query                  # result rows agree across engines?
uv run bench plot query                   # figures/query-{totals,scatter}.pdf + query.csv
```

Results go to `results/query/<run-id>/<program>/{index,egglog,duckdb,umbra}.json[l]`, with the
host, commits and settings in `run.json`; generated queries and exported tables stay in
`build/query/query/<run-id>/`. Umbra runs in the `umbra-bench` container on port 5432, so run
one Umbra job at a time.

## Notes

- The `egglog` variants build from the `egglog` submodule's object store; any
  commit, tag or branch it knows is a valid ref, and unknown refs trigger a fetch.
  Experiments pin a commit so results stay reproducible; move the pin deliberately.
- `systems/egglog/patches/pprof.patch` adds `--pprof <path>` to egglog: a sampling
  profiler (pprof-rs, ~1 kHz, all threads, no perf_event needed) that writes a
  flamegraph `<path>.svg` and sampled stacks `<path>.txt` at exit. Build with
  `bench build egglog <ref> pprof`; worker-thread stacks are truncated at the rayon
  closure boundary, so attribute by leaf function there.
- `--no-dynamic-order` (used by the ablation) is not upstream yet; it lives in
  `systems/egglog/patches/no-dynamic-order.patch`. It keeps the size-based
  ordering of join stages at the start of each query execution and only disables
  the re-sorting of the remaining stages while the join runs.
- egglog 0.5.0 has no `-j`, so multi-threaded configurations of it are `unsupported`.
- Scaling runs are pinned with `taskset` to the CPU list in the experiment
  (`cpus = "0-63"` = the 64 physical cores of this machine; 64–127 are SMT siblings).
