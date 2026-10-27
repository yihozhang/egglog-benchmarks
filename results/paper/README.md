# egglog-cracked paper rerun

All work is isolated in branch `paper/egglog-cracked-20261025` at
`/home/yz489/egglog-benchmarks/.worktrees/paper-cracked-20261025`.
The live checkpoint is [progress.json](progress.json). The experiment queue runs
sequentially; complete runs are frozen as `paper-cracked-20261025` under
`results/<experiment>/`. Original raw logs remain in `runs/<run-id>/logs/`.

## Source version

`egglog-cracked` is the local merge `c1e12aef4907f8ab0febb3568c1e6bae462051b5`:

- origin/main: `43cc88e5abed0567afa75180e27e0805b62e89ff`
- PR 1072: `b58b512e62a3fe855d9dbb13994cbfc700da41b0`
- PR 1056: `174bcc9f034ac7c7379441af0236297601f7198d`
- PR 1074: `4ca4e086ceecf771911469826415e99322c9cab5`
- PR 1075: `9a4c3bd8b13000be3ba4de721fa4035a5f069c3b`

The PRs were merged in that order using local merge commits. Nothing was pushed
or merged on origin. [base.json](base.json) records the pins; the Git bundle
[egglog-cracked-base.bundle](egglog-cracked-base.bundle) preserves the local commits,
with the origin/main commit above as its prerequisite.

To restore the local source commit in another checkout, fetch the prerequisite
from origin, then fetch the saved branch from the bundle (both commands only
update local refs):

```sh
git -C egglog fetch origin 43cc88e5abed0567afa75180e27e0805b62e89ff
git -C egglog fetch ../results/paper/egglog-cracked-base.bundle refs/heads/paper/egglog-cracked-base-20261025
```

Every cracked experiment uses `--no-decomp`. Database snapshot construction and
query execution both pass the same flag to the embedded driver. `egglog-prior`
is egglog v0.5.0 (`1046874bf89e125ea79365ddec556560d2d49a77`); that version has no
tree-decomposition implementation or `--no-decomp` flag. Internal adapter names
still use `egglog` to identify the executable interface.

## Scope

| Experiment | Workloads | Comparisons |
|---|---|---|
| `engines` | Math microbenchmark, 11/12 iterations; Horner-9/10; matmul-320/480/640 | cracked at 1/4/8/16 threads, prior, egg, oatlog, hegg, Metatheory, ggs, ego, Foresight at 1/4/8/16 threads |
| `e2e` | All 42 Hardboiled, 7 Luminal, 32 eggcc programs | cracked vs prior, 1 thread |
| `storel` | Six STOREL kernels | cracked with the STOREL Rust extension vs egg; both backoff |
| `scaling` | Math 11, Horner-9, matmul-320 | cracked and Foresight, each at 1/2/4/8/16 threads |
| `scaling-real-world`, `scaling-storel` | All real-world workloads above | cracked, 1 and 8 threads |
| `storel-schedule` | The same six STOREL kernels | rewrite batch 1/2/4/8, each at 1 and 8 threads |
| `ablation`, `ablation-storel` | All seven algebraic instances and all 87 real-world programs/kernels | cracked, no-DVO size order, no-DVO refinement order; 1 thread |
| `query` | Every translatable rule in all seven Luminal programs | cracked, DuckDB, Umbra |

The six STOREL kernels are `mmm-sum-2nd`, `mmm-2nd`, `batax-1st`, `mttkrp-2nd`,
`ttm-2nd`, and `batax-2nd`. STOREL is never evaluated on prior.

The conditional size pilots completed: Horner-10 approximately 2.7 s,
matmul-320 approximately 0.85 s, and matmul-640 approximately 7.19 s on cracked
with one thread. Pilot rows are excluded from all paper figures. Matrix sizes
480 and 640 are both included in the final run at the user's request. Scaling
uses 320 so the Foresight run remains manageable.

## Real-world result-size checks

All 486 end-to-end repetitions completed successfully. The size checker reports
32 cross-version differences, all in eggcc; Hardboiled and Luminal have no
reported differences. The historical `griffin-20261019` no-decomposition vs
v0.5.0 comparison also reports differences for those same 32 eggcc programs.
The prior engine's table counts match its historical counts exactly. Cracked
matches the historical no-decomposition counts on 76/81 programs, with changes
on five eggcc programs. Exact evidence is retained in
[e2e-size-comparison.json](e2e-size-comparison.json).

The eggcc inputs contain order-sensitive extraction ties (`Smaller` chooses its
first argument at equal cost) and use extracted terms in later rules. These
measurements compare the original schedules' runtimes; they do not establish
identical intermediate work, identical final e-graphs, or extracted-program
equivalence across versions. The figures and CSVs flag these size differences.
Do not describe this check as full semantic equivalence verification.
Across algebraic engines, size comparisons only cover tables reported under
common names. The audit records comparisons without common reported tables
separately instead of treating them as verified matches.

The cross-thread/repetition audit also finds table-size variation in 24 eggcc
programs within the cracked-only 1/8-thread run. Algebraic scaling has no such
size differences. The real-world scaling figures therefore describe the
runtime of each configuration's original schedule, and flag the affected pairs.

## Two static-order baselines

Both origin/main and the older benchmark pin `b9800506` called
`sort_plan_by_size` before execution to compute a full initial order. The older
no-DVO patch retained this initial sort. It is incorrect to describe that old
baseline as selecting only its first stage.

After the requested PR merges, initialization calls `select_next_stage` only
for the root, deferring later choices until filtered residuals exist. The new
patch supplies two static baselines on this merged base:

1. `no-DVO`: invoke `select_next_stage` at **every** position before any stage
   executes, using initial cardinalities; never reorder during execution.
2. `no-DVO-refinement`: use the full greedy initial sorter copied from the
   pinned origin/main. Its key is `(-refinement count, input rows, -number of
   intersected relations)`; update refinement counts after each selected stage.
   This prioritizes stages connected to already selected relations. Never
   reorder during execution. The extra CLI flag is `--static-order-refinement`,
   which requires `--no-dynamic-order`.

All three variants use the same patched binary. The ordinary cracked path
retains the merged implementation's dynamic behavior. The two figures use
identical cracked measurements and normalize separately to their static baseline
at 1.0x. Failed, OOM, or timed-out pairs are excluded from numerical speedups and
retained with explicit statuses in CSVs and detailed figures. Coverage counts
are shown beside geometric means. If the static baseline times out before any
successful repetition and cracked completes, a hollow triangle shows the timeout budget divided by cracked's
median runtime as a censored lower bound; it is excluded from the geometric
mean and is never treated as an observed static runtime.
The two geometric means can therefore cover different completed subsets; their
quotient is not a paired estimate of the refinement heuristic's benefit.

Static ordering also changes reported table sizes in seven eggcc programs (the
complete list is in [audit.json](audit.json)). For example,
Ackermann differs in `Smaller`; ADI also differs in `TCPair`, `TermConcat`, and
`TermGet` with the size-only order. These counts are stable across each
configuration's three repetitions. The figures and CSVs flag differing pairs;
these are original-schedule runtime comparisons, not a demonstration that every
ordering strategy performs identical intermediate work.

The patch is [no-dynamic-order.patch](../../systems/egglog/patches/no-dynamic-order.patch).
To validate and build it, run `bash scripts/paper/verify-ablation.sh`.
The final core test run passed **201 tests** with `--test-threads=1`, including
full-order and refinement-priority regression tests. An earlier concurrent test
run had one scheduler-metrics assertion failure; that assertion passed alone
on both the exact base and the patched source, and the serial full suite passed.
All logs are retained under `logs/paper/`.

## STOREL schedule design and interpretation

The control preserves the original backoff schedule exactly:

```lisp
(run-schedule
  (run-with bo phase1) (saturate (run aux))
  (run-with bo phase2) (saturate (run aux)))
```

Batch K replaces each `run-with` with `(repeat K (run-with bo phaseN))`.
Thus each rewrite phase executes K times before its aux refresh. Rules, phase
order, initial aux saturation, backoff policy/state, and per-kernel node target
are unchanged. We test K = 1, 2, 4, 8 at both 1 and 8 threads with three runs.
Generated schedules are saved in the corresponding run log directories.

These are schedule interventions, so equal targets do **not** imply identical
explored e-graphs. Backoff bans, delayed analysis, extraction-based substitution,
apply order, and overshoot can change actual work. Every comparison retains
`report.nodes`, `node_limit`, `iterations`, and `stop_reason`; the accompanying
work figure shows reached nodes / target. Timing differences must be read
alongside these work measurements, especially if a run stalls below its target.

At K=4, `mttkrp-2nd` stalls at 25,728 nodes (target 1,000,000) at both thread
counts. A separate diagnostic extended the quiet allowance from 5 to 2,000
steps; it still stopped at the same size after 2,027 outer iterations. Its
report is in `logs/paper/storel-stall-diagnostic/`. These below-target runs remain
in the work figure and CSV, but are excluded from all numerical speedups. The
diagnostic is excluded from the timing matrix. This is a material outcome of
changing the schedule, not a successful performance improvement.

## Measurement policy

- Host: Griffin, AMD Ryzen Threadripper PRO 7985WX, 64 physical cores, 503 GiB RAM.
- Matrix runs bind to physical CPUs `0-7,32-39` (16 cores on NUMA node 0).
  Container workers and JVM GC threads receive this affinity too.
- Runs are sequential, in benchmark/variant/thread/repetition order.
- `engines`: five repetitions. Other matrix experiments: three repetitions.
- Per-run limit: 600 s and 32 GiB; STOREL experiments: 900 s and 32 GiB.
  The repetition group ends at the first unsuccessful outcome.
  Native workers use systemd `MemoryMax=32768M` and `MemorySwapMax=0`.
  Container workers use `--memory 32768m`; their swap setting is the container
  runtime default. The host has an 8 GiB swap file. Thus the common limit is
  resident memory, and uniform swap disabling should not be claimed.
- Figures use median `wall_s`, with the raw repetitions and statuses retained.
  Native engines include process startup/parsing. JVM/Julia port measurements
  exclude VM startup and their existing separate warmup, as in the original
  harness. Foresight keeps its default G1 collector; thread count controls
  equality-saturation work, while GC can use the allowed CPU set.
- Query comparison follows the existing timing definitions: egglog search,
  apply and merge exclude rebuild; SQL optimize/execute times are separate.
  DuckDB timings and Umbra EXECUTE/ad-hoc timings use three timed repetitions
  after one warmup, with 60 s statement timeouts. Umbra PREPARE is timed once
  per query. Egglog's query driver uses one pass, and its existing setup has no timeout.
  These query-specific limits are recorded in each manifest.
  The fresh run translated all 3,437 queries and had no output row-count
  mismatches. Umbra recorded 57 errors: 56 server disconnections and one
  `could not lock the schema` error on the empty `gemma4_moe/r202` query.
  Five nonempty queries failed in Umbra; the shared successful nonempty
  comparison therefore contains 727 of 732 nonempty queries.
- Dedicated `egglog-paper-cracked/*` image tags and an isolated localhost Umbra
  port (55432) avoid changing the original benchmark images or container.
- During user-requested preparation, the queue was paused after letting the
  active adapter finish. Its own `wall_s` remained unchanged; the queue's
  ancillary `harness_wall_s` can include the pause. Pause records are in
  `logs/paper/`; those harness times are never plotted.

## Summary statistics and historical reference

The follow-up [real-world aggregates](real-world-speedups.json) are reproduced
with `scripts/paper/application_speedups.py`. They distinguish equal weight
per application suite from equal weight per individual benchmark, and keep
the egglog-prior and egg baselines separate.
[Oatlog speedups](oatlog-speedups.json) are reproduced with
`scripts/paper/oatlog_speedups.py`, using five-run medians separately for cracked
at 1, 4, 8, and 16 threads against single-thread oatlog. This summary can read
those completed configurations while other engine configurations are still
running; rerunning it after completion also verifies the sealed run bundle.

For the fresh frozen paper selection, run
`taskset -c 16-23 .venv/bin/python scripts/paper/query_speedups.py` after the
query stage finishes. It writes `query-speedups.json`, a per-program CSV,
and the individual-query CSV here. The results report distinguishes the
per-query geometric mean from the ratio of summed preparation times.
Use `--metric execute` for the corresponding runtime statistics, which exclude
preparation. For the historical run use the same option on
`scripts/paper/historical-query-speedups.py`; the runtime artifacts are named
`historical-query-runtime-speedups*`.
The runtime summary excludes compilation/preparation. Egglog uses search +
apply + merge, excluding rebuild. Some empty queries have zero recorded
egglog execution time; their individual speedup ratios are undefined, so the
all-query runtime geometric mean is reported as null. The shared nonempty
cohort has positive times throughout and supports the reported geometric mean.

[historical-query-speedups.json](historical-query-speedups.json) summarizes the
October 20 run at `b9800506`, using its `--no-decomp` measurements. It records
per-query geometric and arithmetic means, ratios of total optimization times,
and the geometric mean of the seven per-model total-time ratios. The shared
successful nonempty cohort contains 727 queries out of 732 nonempty queries;
five nonempty queries had Umbra errors. All successful output row counts agree.
The run contains 3,437 translated queries in total, with no untranslated rules.
Reproduce this reference analysis with
`.venv/bin/python scripts/paper/historical-query-speedups.py`; per-model and
per-query CSVs accompany the JSON. Its source bundle and commit are recorded in
the output.

## Reproduce and inspect

From the worktree:

```sh
.venv/bin/python scripts/paper/run.py
.venv/bin/python scripts/paper/plot.py
.venv/bin/python scripts/paper/audit.py
.venv/bin/python scripts/paper/verify.py
.venv/bin/python -m harness summary engines --selection paper-cracked-20261025
.venv/bin/python -m harness check scaling --selection paper-cracked-20261025
```

The queue checkpoints completed experiments and verifies their bundles when
restarted. It does not resume an interrupted experiment's partial matrix.
Selections are immutable; a deliberate rerun should use a new selection name.
The first, unfinished engines run was excluded after Math 12 exposed an adapter
bug: its numeric suffix was passed as `--instance 12`, but hegg, Metatheory, and
ego require `--iters 12`. The shared resolver was corrected, all five container
systems passed a one-iteration CLI probe, and all 98 Python tests passed. The
entire engines matrix was restarted before creating its selection. The excluded
run, original errors, and before/after source hashes are recorded in
[engine-rerun.json](engine-rerun.json); the other eight completed experiments
were preserved. The probes are diagnostic and are not paper measurements.
Python and package versions are recorded in
[python-environment.json](python-environment.json). The database comparison
uses DuckDB 1.5.6 and psycopg 3.3.6. A fresh environment needs the project's
`query` optional dependencies (`uv sync --extra query`); pin the versions in
that environment file when reproducing this particular run.

Figures are under `figures/paper/`: `engines.pdf` and separate PDFs for each
algebraic instance, `real-world.pdf`, `storel-backoff.pdf`,
`scaling-algebraic.pdf`, `scaling-real-world.pdf`, `storel-schedule.pdf`,
`storel-schedule-work.pdf`, `ablation.pdf`, `ablation-refinement.pdf`, and
`query-{totals,scatter}.pdf`. PNG previews and CSVs accompany the PDFs.
The plotting script only emits a figure once all of its source selections exist.
`verify.py` requires all ten experiments to be complete. It verifies frozen run
bundles, pins, ablation flags, all seven algebraic instances, the full
1/2/4/8/16-thread scaling grid, individual and detailed figures, figure checksums,
and PDF labels. Nonzero
result-size checks remain recorded findings; this artifact verification does
not convert them into semantic-equivalence passes.
