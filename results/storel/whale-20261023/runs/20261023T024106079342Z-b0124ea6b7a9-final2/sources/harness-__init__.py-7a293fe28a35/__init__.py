"""Benchmark harness for the egglog system paper.

Layout (all paths relative to the repository root):

  benchmarks/<suite>/            one directory per benchmark suite
  systems/<system>/build|run     adapters: how to build a system at a ref and run one benchmark
  experiments/<name>.toml        the matrix for one experiment (variants x benchmarks x threads)
  results/<experiment>/*.jsonl   one row per run, appended; the source of truth for plots
  results/<experiment>/logs/     raw stdout/stderr of every run (not committed)
  figures/                       output of `bench plot`
"""
