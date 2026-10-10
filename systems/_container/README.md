# Container-based systems

For baselines whose toolchain is not on the host (Scala, OCaml, Haskell, Julia, Common
Lisp, ...), a system is a directory `systems/<name>/` with

| file | role |
|---|---|
| `system.toml` | `port_dir = "<name>"`, `repo = "<git url of the baseline>"`, `container = true`, optional `threads = true` (the engine takes `--threads N`), `notes = """..."""` |
| `Containerfile` | installs the toolchain, clones `repo` at build arg `REF`, copies and builds the ports |
| `entry.sh` | entrypoint inside the image: `entry.sh <suite> <name> [flags]` runs one port |
| `build` | symlink to `../_container/build.py` |
| `run` | symlink to `../_container/run.py` |

Ports live in `benchmarks/<suite>/<name>/` like for every other system. A benchmark
`base-<N>` (e.g. `horner-7`) is served by a port file with stem `base-<N>`, or else by a
port with stem `base` which then receives `--instance N`; so one port can cover every
instance size of a suite.

## Build

`build <ref>` keeps a clone of `repo` under `build/<name>/repo` to resolve `ref` to a
commit, stages a small build context in `build/<name>/<commit>/context/`:

    context/system/   the contents of systems/<name>/   (Containerfile, entry.sh, helpers)
    context/ports/<suite>/...   the contents of benchmarks/<suite>/<name>/ (symlinks dereferenced)

and runs `podman build --build-arg REF=<commit> -t egglog-bench/<name>:<commit12>`.
The Containerfile therefore does `COPY system/ /bench/` and `COPY ports/ /bench/ports/`,
compiles the ports there, and sets `ENTRYPOINT ["/bench/entry.sh"]`.
`build.json` records the image name; it is rebuilt (cheaply, cached) on every `bench run`.

## Run

`run` executes

    podman run --rm --network=none --memory ${BENCH_MEMORY_GB}g --timeout ${BENCH_TIMEOUT_S} \
        egglog-bench/<name>:<commit12> <suite> <name> [flags]

(`BENCH_CPUS` is not applied: rootless podman has no cpuset controller here, and the
container engines are single-threaded.)

captures stdout/stderr, and builds `report.json` from the port's stdout. For a system
with `threads = true` the port receives `--threads N` for N > 1; otherwise threads other
than 1 are reported as unsupported.

## Port protocol (stdout)

A port runs the benchmark program's workload: the same rules and start terms, and either
the same number of iterations (`(run N)`) or, for `(run-schedule (saturate (run)))`
programs, iterations until one makes no change to the e-graph (no new e-node, no merge;
that final no-change iteration counts, as it does in egglog). One iteration = match all
rules on the current e-graph, apply all matches, rebuild; no node/time limits, no backoff
scheduling, no extraction. It prints

    iterations <N>         iterations performed (saturating programs: including the final no-op one)
    run_s <seconds>        time of the iterations only, as the engine reports it (reference only)
    wall_s <seconds>       optional, JIT languages only: the port's own wall-clock measurement
                           after a JIT warm-up, covering e-graph construction, the iterations
                           and the size count (VM startup and warm-up excluded)
    (
     (Add 641743)          e-nodes per constructor, egglog names, egglog (print-size) format
     (Mul 345075)
     ...
    )

Anything else may go to stderr. `entry.sh` additionally prints `wall_ns <int>` around the
port process (`report.process_wall_s`). `report.wall_s` is the headline number: the process
wall time, or the port's own `wall_s` when it prints one. Measurement policy: wall-clock
time measured by our code (harness, entry.sh or the port), never the engine's own report.
