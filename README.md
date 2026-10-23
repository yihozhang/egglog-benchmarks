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
uv run bench list
uv run bench list ablation
uv run bench run ablation --machine server-a --cpus 0-15
uv run bench runs ablation
uv run bench run e2e -b pointer-analysis --tag analysis-only  # all variants, loading excluded
uv run bench inspect <run-id>
uv run bench summary ablation                 # one latest complete run
uv run bench check ablation --run <run-id>

# Freeze a reviewable result set; this does not create a Git commit.
uv run bench select ablation --runs <run-id> --as paper-v1
uv run bench plot ablation --selection paper-v1 --out ../egglog-system-paper/figures
```

Every invocation writes an independent `runs/<run-id>/` bundle, ignored by Git.
The ID uses UTC time and a random suffix, with an optional `--tag`. A bundle has
`run.json`, `measurements.jsonl` (matrix experiments), source/config snapshots,
and `logs/`. Query experiments keep their per-program payloads in the same bundle
format. Runs are marked `running`, `complete`, `failed`, or `interrupted`.

`summary`, `check`, and `plot` default to **one latest complete run**, using its
captured configuration. An exact ID or an unambiguous prefix/tag selects another
run. `--run all` for matrix experiments only combines compatible, nonoverlapping
configuration groups: it never silently replaces a cell with its latest value.
Query reports use one run. `bench inspect` also works on failed/interrupted runs.

## Ad hoc experiments and committed results

Keep temporary configs, scripts and patches in ignored `scratch/<topic>/`:

```sh
uv run bench run scratch/dvo-check.toml --machine server-a --tag exploratory
```

A TOML path works wherever an experiment recipe is accepted. For egglog, a variant
can use a named patch such as `patches = ["no-dynamic-order"]`, or an explicit file
such as `patches = ["./candidate.patch"]`, resolved relative to the recipe.
The configuration and applied patch contents are captured in the run bundle;
removing scratch files later does not remove the recorded evidence. Reusable
recipes, scripts and patches should be moved into the tracked experiment/system
layout deliberately.

`bench select <experiment> --runs <id> [<id> ...] --as <version>` creates
`results/<experiment>/<version>/`. It preserves the selected raw measurement rows,
compact source run bundles, the frozen presentation/analysis configuration, and
`selection.json` with exact source IDs, coverage, provenance status and checksums.
Raw logs are omitted. Review and commit this directory with normal Git commands.
Selection names cannot be overwritten; create a new version to change the numbers.
Use `--selection <version>` for paper figures so later exploratory runs cannot
change them. Imported modern runs can be selected by experiment name even when
the original scratch recipe is absent. Both selected and individual modern-run
reports remain usable without that recipe. `--metric` can explicitly override a
report's metric; `select --metric` freezes a different metric in a new selection.

Selection checks every run's planned cases and repetitions. A terminal failure or
timeout can end a repetition group early; its outcome is preserved, and a group
with any non-success is excluded from ordinary speedups. Selections may cover a
subset of the canonical experiment: the exact coverage is recorded, so review it
before using it as a complete paper table. Overlapping groups from different runs
are rejected; choose which complete group to use instead of cherry-picking
repetitions. The first version selects one query run at a time.

For a partial rerun that overlaps an older run, `select --groups choices.json`
explicitly chooses whole configuration groups from each source. The JSON maps each
exact source run ID to a nonempty list of objects with `suite`, `benchmark`,
`variant`, and `threads`. List every source passed with `--runs`; all repetitions
of each chosen group are retained. The selection records these choices and copies
all original source measurements, including excluded groups, for review. Without
`--groups`, overlapping runs remain an error. Legacy sources still require
`--allow-legacy`; filtering does not upgrade their provenance.

## Multiple machines

Run each machine independently, with a descriptive `--machine` alias. The alias
is recorded alongside detected hardware, kernel, affinity and runtime settings;
reusing an alias does not make incompatible environments equivalent. Use `--cpus`
to override a matrix recipe's CPU binding for the current machine.

```sh
# On the measurement machine:
uv run bench export <run-id> --out /tmp/run.tar.gz
# Copy the archive to the review machine using your normal file-transfer method.
uv run bench import /tmp/run.tar.gz
uv run bench select ablation --runs <run-id> --as server-a-v1
uv run bench summary ablation --selection server-a-v1
```

Imports verify payload checksums, preserve the original machine identity, and are
idempotent for identical bundles. A conflicting run ID is rejected. Machines never
append to a shared results file. Keep each benchmark's baseline, alternatives and
thread-count comparisons on the same machine. Reporting and selection reject mixed
machines/runtime settings, different input identities, and changed code or ports
under the same variant label. Create separate selections for different machines.

Manifests record resolved settings after CLI overrides, exact engine commits,
ordered patch hashes, adapter/source snapshots, input identities, known executable
hashes, host/runtime settings and the observed toolchain. Large input datasets are
identified by content hashes rather than embedded. Adapter metadata is retained;
current compiler/source observations cannot prove how a previously cached binary
or opaque container was built. These limits are recorded in the build provenance.
Remote scheduling, automatic sharding and resume are not implemented in this version.

Existing committed `results/<experiment>/*.jsonl` remain readable with `--run <id>`;
they are excluded from automatic latest/all selection because completion is unknown.
They lack the new completeness/provenance information. Selecting them requires
`--allow-legacy`, which records `legacy-incomplete` in the selection rather than
inventing missing metadata. Legacy and modern rows cannot be combined. For old
query directories, specify the run ID explicitly because completion is unknown.

The retained Griffin measurements are frozen as `griffin-20261019` for `e2e` and
`ablation-parallelism`, and `griffin-20261016` for `ablation`, `engines`,
`engines-small`, and `math-microbenchmark-12`. For example:

```sh
uv run bench plot e2e --selection griffin-20261019
uv run python scripts/plot-ablation-parallelism-benchmarks.py --selection griffin-20261019
```

These selections retain their original rows and `legacy-incomplete` provenance.
Their `migration.json` files record the source choices. Original local logs,
smoke tests, and abandoned prototypes are preserved under ignored
`scratch/legacy-migration-20261019/original/`, with a relocation/checksum manifest
one directory above. The selected ablation pointer measurements used `(run 100000)`;
the later e2e pointer measurements used `(run-schedule (saturate (run)))`.

## Layout

| path | what |
|---|---|
| `benchmarks/<suite>/*.egg` | one benchmark per file, or a `suite.toml` naming one program and a glob of fact directories |
| `benchmarks/<suite>/<system>/` | ports of the suite to other systems (a system supports a benchmark iff its port exists) |
| `systems/<system>/build <ref>` | builds the system at a git ref, prints the build dir, writes `build.json` there |
| `systems/<system>/run <build-dir> <program> <fact-dir|-> <threads> <out-dir> [flags]` | runs one benchmark; writes `stdout`, `stderr`, `report.json`; exit 3 = unsupported |
| `experiments/<name>.toml` | variants, benchmarks, thread counts, reps, timeout, CPU pinning, plot kind, baseline |
| `scratch/<topic>/` | temporary recipes, scripts and patches (ignored) |
| `runs/<run-id>/` | independent run bundle, measurements, provenance and logs (ignored) |
| `results/<experiment>/<selection>/` | explicitly selected measurements and provenance for Git review |
| `results/<experiment>/*.jsonl` | legacy committed measurements (read-only compatibility) |
| `figures/` | output of `bench plot` (PDF + PNG + CSV) |
| `build/` | git worktrees and cargo targets, keyed by commit (not committed) |
| `harness/` | the `bench` CLI: `config.py` (layout), `runner.py`, `results.py`, `plot.py`, `cli.py` |

## Adding things

- **A benchmark**: drop a `.egg` file into a suite directory. Files a suite's programs `include`
  (like `benchmarks/luminal/luminal-header.egg`) are listed under `exclude` in its `suite.toml`.
  A program that never saturates can end with a marker line `;; BENCHMARK_NODE_LIMIT <N> <table> ...`
  followed by one step (e.g. `(run 1)`): the egglog adapters then drive the REPL, repeating the step
  until the listed tables hold N rows (`report.stop_reason` = `node-limit`, or `stalled` once nothing
  changed for more steps than had run before plus five, the allowance a back-off scheduler's bans
  need); `wall_s` stays the whole process.
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
 "log_dir": "logs/…", "host": {...}, "machine_id": "server-a",
 "environment_id": "…", "config_id": "…", "build_id": "…", "input_id": "…", "port_id": "…",
 "harness_commit": "a85aa6e…", "timestamp": "…"}
```

`status` is `ok`, `fail`, `timeout`, `oom` or `unsupported`. `wall_s` is the
adapter's measured wall-clock interval, identified by `timing_scope` (`process`
by default, `analysis` for egglog pointer analysis). `harness_wall_s` is measured
around everything, including the adapter and cgroup setup; the other
`report.*` fields are the system's own numbers (for egglog, from `(print-stats)`
and `(print-size)` appended as a trailer file). Node-limited runs (STOREL) add
`report.iterations`, `report.stop_reason` (`node-limit` or `stalled`) and
`report.nodes`, the e-node count at the stop, which can exceed the limit by up to one
iteration's growth.

Every run gets `timeout` seconds and `memory_gb` GB (defaults 600 s and 32 GB,
per experiment or `--timeout`/`--memory-gb` on `bench run`). The memory cap is a
transient systemd user scope (`MemoryMax`, no swap), so an over-budget run is
OOM-killed and recorded as `oom`; without systemd it falls back to an
address-space rlimit.

## Systems

| system | how it is built | pinned ref in `experiments/engines.toml` | notes |
|---|---|---|---|
| `egglog` | host cargo, any ref of the submodule, optional patches | b9800506 (branch yihozhang-bench-20261016), v0.5.0 | `-j`, `--no-decomp`, `--no-dynamic-order` (patch) |
| `egglog-experimental` | host cargo; egglog-experimental 61e68719 with the variant's egglog checkout substituted through a Cargo `[patch]` | (no experiment at the moment) | port = the program in the experimental scheduling language, e.g. `(let-scheduler bo (back-off))` + `(run-with bo ...)` |
| `egglog-storel`, `egglog-storel-backoff` | host cargo; `systems/egglog-storel/crate` (egglog-experimental's e-graph + a Rust extension) on the variant's egglog | b9800506 (`experiments/storel.toml`) | STOREL's beta reduction and index shifting in Rust on the smallest term of a class, kept as a value of the extension's `Frozen` base sort (`thaw` adds the result); the suite's programs, and the same under the back-off scheduler; the rules calling `thaw` are `:unsafe-seminaive` |
| `egg` | host cargo; Rust port per benchmark | v0.11.0 | `SimpleScheduler`, no limits (math, horner, matmul-assoc); the `storel` port takes `--scheduler backoff\|simple` (STOREL's `BackoffScheduler` or `SimpleScheduler`) and stops at the kernel's node limit |
| `oatlog` | host cargo; port = egglog program compiled by the proc macro | 6b73396 (main) | strict mode; relaxed mode exceeds 32 GB |
| `hegg` | container (GHC 9.6); Haskell port | v0.5.0.0 | driver loop in the port (hegg's own caps iterations at 30 and bans rules); the port canonicalizes the e-matching database because hegg's leaves stale ids |
| `metatheory` | container (Julia 1.11); Julia port | v2.0.2 (latest release; no 3.x release) | `SimpleScheduler`, limits disabled, JIT warm-up excluded from `run_s` |
| `ggs` | container (SBCL 2.5); Common Lisp port of the `ggs/eqsat` system | 2ad09810 (master, no tags) | engine's own `run-rewrites` driver, no match limit; standalone executable |
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
Pointer analysis is an explicit exception: its suite sets `egglog_timing = "analysis"`.
For every egglog variant, the adapter starts one persistent CLI process, loads the
program before `;; BENCHMARK_TIMING_START`, and waits for completion. It measures the
request that runs the commands through `;; BENCHMARK_TIMING_END` until completion.
Current versions use `--mode interactive` and `(done)` acknowledgments. Egglog 0.5.0
uses its native `(set-option interactive_mode 1)` completion messages. Setup and
reporting suppress intermediate acknowledgments and restore one at the phase boundary;
the timed saturation schedule has one native `(done)` acknowledgment. The legacy path
also monitors stderr because parse/type errors may have no stdout acknowledgment.
It requires the marked interval to contain the single supported saturation schedule,
so multiple commands cannot accidentally end the timer early. No engine patch is needed.
Currently this interval is exactly `(run-schedule (saturate (run)))`: runtime query planning, querying,
actions, merge and rebuild remain included. Process startup, CSV loading, declarations
and rule registration/compilation, final size/stat printing and shutdown are excluded.
The interval includes the small include-command read/parse and pipe round-trip overhead;
it is an independently measured wall time, not a sum of egglog's phase timers.

Each such row records `timing_scope = "analysis"`. The report also keeps
`analysis_wall_s` (equal to `wall_s`), `setup_wall_s` (startup through setup completion)
and `process_wall_s` (the complete engine process). CPU times and peak RSS still cover
the whole process, including loading. Logs contain `setup.egg`, `analysis.egg`,
`finish.egg` and the exact interactive `stdin.egg` transcript. Loading and reporting
remain subject to the same full-run timeout and memory limit. Other suites retain
their existing timing. Pointer-analysis timing applies to all three `e2e` variants: `egglog`,
`egglog-0.5.0`, and `egglog-no-decomp`. Old rows without `timing_scope` mean process time; summaries
reject a partial rerun that mixes process and analysis timing for one benchmark.

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

The small instances (horner-5/6/7, matmul-assoc-20/40/80: fastest engine under 100 ms) are a
separate experiment, `experiments/engines-small.toml`, with the same variants (keep the two
in sync), so `bench plot engines` and `bench plot engines-small` give one figure each.

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

## STOREL: tensor kernels with binders

`experiments/storel.toml` runs STOREL's optimizer (SIGMOD'23; the egg 0.6 implementation
from the slotted e-graphs artifact: 44 rewrite rules over SDQL with de Bruijn indices, and
beta reduction / index shifting implemented as custom appliers that substitute the *smallest
known term* of an e-class) on its ten evaluation kernels; `benchmarks/storel/gen.py`
generates every port from the original inputs and documents the encoding. The six kernels
that take more than 100 ms are benchmarked. Because the appliers substitute whichever term is
smallest *at the moment they run*, the e-graph a run explores depends on the engine's apply
order (egg 0.6 and 0.11 differ 25x on `mmm-2nd` with identical code), so no run goes to
saturation: every kernel stops after the iteration that reaches a fixed number of e-nodes
(10K, 150K or 1M) and the metric is the time to grow the e-graph to that size. Two
head-to-head comparisons, each between the original code on egg 0.11 and egglog with
`systems/egglog-storel`, a Rust extension that does what STOREL's appliers do (the smallest
term of a class is computed by rules; the extension substitutes or shifts it and adds the
result): `egg-simple` vs `egglog-rust` (every rule applied every iteration: egg's
`SimpleScheduler`, egglog's plain `run`) and `egg-backoff` vs `egglog-rust-backoff` (STOREL's
`BackoffScheduler` and egglog-experimental's `back-off`, both with match limit 1000 and ban
length 5; egglog counts a rule's new matches per step, so bans fall at different times).
`scripts/plot-storel.py --run <id>` draws both with the e-graph size each run reached. The
egglog ports choose the same smallest term (ties broken by a structural hash) and reach
identical e-graphs on the saturating stage-1 kernels; `egg` keeps STOREL's "first seen"
tie-breaking. On the
benchmarked kernels the variants stop on different e-graphs (so `bench check storel` reports
mismatches by design); `report.nodes` records the size each run actually reached, which can
exceed the limit by one iteration's growth (egglog's iterations are few and large, egg's many
and small).

## Query-only comparison with DuckDB and Umbra

`experiments/query.toml` (`kind = "query"`, driven by `harness/query.py`): every rule of every
luminal program, run once on the program's final state, in egglog and as SQL in DuckDB and
Umbra. Translation and timing rules are in `systems/egglog-query/system.toml` (`notes`).

```
uv sync --extra query                     # install DuckDB and the PostgreSQL client
uv run --extra query bench run query      # build driver, export, translate, run all engines
uv run --extra query bench run query -b llama -v duckdb # one program, only DuckDB (plus egglog)
uv run bench summary query                # per program: summed optimize/execute, failures
uv run bench check query                  # result rows agree across engines?
uv run bench plot query                   # figures/query-{totals,scatter}.pdf + query.csv
```

Results go to `runs/<run-id>/<program>/{index,egglog,duckdb,umbra}.json[l]`, with the
host, commits, effective settings and completion state in `run.json`; generated queries
and exported tables stay in `build/query/query/<run-id>/`. Query runs support the same
`runs`, `inspect`, `export`, `import`, and `select` commands. `--threads`, `--reps`, and
`--timeout` control SQL measurements; egglog remains one thread and one pass without
a harness timeout, explicitly recorded in the manifest. Query CPU/memory overrides
are rejected because they cannot consistently constrain the container backend. Umbra
currently requires one thread. Umbra runs in the `umbra-bench` container on port 5432, so run
one Umbra job at a time. The container executable comes from `CONTAINER_ENGINE` when set;
otherwise Podman is preferred if installed, with Docker as the fallback. The selected
engine must have a running daemon/service where required. Umbra uses the
`docker.io/umbradb/umbra:latest` image; its resolved ID and digests are recorded per run.

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

## Query parallelism ablation

`experiments/ablation-parallelism.toml` runs the standard suite with three competitors:
vanilla `egglog`, `inter-query-only`, and `inter-query-top-level`. It uses 1/2/4/8
threads, three repetitions, and CPUs 0–7 (physical cores on NUMA node 0). All three
share egglog commit `4b5686bed78cf7cf153aebf8e6a2abed1840afee` and the
`query-parallelism` patch, matching the Gemma pilot. Other experiments retain their
existing pins. The patch only adds a top-level query partition toggle; recursive
query forks use the existing fork-depth control. Merge, rebuild, index construction,
containers, and action scheduling keep their existing policies.

Variant `env = { NAME = "value" }` entries override the run-time environment of
that variant's adapter and child process. They do not affect the build, and are
recorded in the frozen configuration, identity checks, each result row and the
run's `env.json` log. The three query modes set
(fork depth, top partition enabled) to (2, 1), (0, 0), and (0, 1), respectively.

```sh
uv run bench run ablation-parallelism
uv run bench run ablation-parallelism -b luminal/gemma -r 1 --tag smoke
uv run bench run ablation-parallelism -b pointer-analysis --tag analysis-only
uv run bench summary ablation-parallelism
uv run bench summary ablation-parallelism --metric report.search_apply_s
uv run bench check ablation-parallelism
uv run bench plot ablation-parallelism
```

The per-suite figures are specific to this parallelism ablation. For a frozen
selection, use `uv run python scripts/plot-ablation-parallelism-benchmarks.py --selection VERSION`.
Other experiments retain their existing figure layouts. The plotting scripts use
captured configurations and the same explicit selection rules as `bench plot`.

For a completed pointer-only rerun, generate its per-benchmark wall-time figure with
`uv run python scripts/plot-ablation-parallelism-benchmarks.py --run RUN_ID --suite pointer-analysis`.
The loading-excluded figure uses an `analysis-wall-time` filename, preserving older figures.

The headline scaling plots use process wall time except for pointer analysis,
which excludes setup/loading as described above. Search-and-apply, merge, and rebuild times remain in each row
for analysis. The runner uses its usual sequential benchmark/variant/thread/repetition
order; this full-suite experiment does not add a warmup or randomize that order.

## Harness tests

```sh
uv run python -m unittest discover -s tests -v
```

Tests use temporary fixtures, mocked builds, and a tiny adapter subprocess; they do
not launch the benchmark suite. They cover run lifecycle/provenance, compatibility checks, frozen selections,
query payload completeness, and portable archive validation.
