"""Loading of suites, systems and experiments from the repository layout."""

from __future__ import annotations

import fnmatch
import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCHMARKS = ROOT / "benchmarks"
SYSTEMS = ROOT / "systems"
EXPERIMENTS = ROOT / "experiments"
RESULTS = ROOT / "results"
RUNS = ROOT / "runs"
FIGURES = ROOT / "figures"


@dataclass(frozen=True)
class Benchmark:
    suite: str
    name: str
    file: Path  # the egglog program
    fact_dir: Path | None = None  # directory for `input` commands, if any
    egglog_timing: str = "process"  # process, or analysis between explicit program markers

    @property
    def id(self) -> str:
        return f"{self.suite}/{self.name}"

    @property
    def dir(self) -> Path:
        return BENCHMARKS / self.suite


def load_suite(suite: str) -> list[Benchmark]:
    """A suite is a directory under benchmarks/.

    By default every `*.egg` file is one benchmark named after its stem; a `suite.toml`
    may list `exclude` globs (e.g. a shared header that programs `include`). It can
    instead name a single `file` and a glob of `fact_dirs`, in which case there is one
    benchmark per fact directory (named after the directory); or it can list `pairs` of
    `{file, facts}` (program x dataset, benchmark `<stem>-<facts>`, facts under
    `<suite>/facts/<facts>` unless `facts_root` says otherwise).
    """
    d = BENCHMARKS / suite
    if not d.is_dir():
        raise SystemExit(f"no such suite: {suite} ({d})")
    cfg_path = d / "suite.toml"
    cfg = tomllib.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    egglog_timing = cfg.get("egglog_timing", "process")
    if egglog_timing not in ("process", "analysis"):
        raise SystemExit(f"suite {suite}: unknown egglog_timing {egglog_timing!r}")
    if "pairs" in cfg:
        root = d / cfg.get("facts_root", "facts")
        out = []
        for pair in cfg["pairs"]:
            file = d / pair["file"]
            if not file.exists():
                raise SystemExit(f"suite {suite}: {file} does not exist")
            out.append(Benchmark(suite, f"{file.stem}-{pair['facts']}", file, root / pair["facts"], egglog_timing))
        return out
    if "file" in cfg:
        file = d / cfg["file"]
        if "fact_dirs" in cfg:
            dirs = sorted(p for p in d.glob(cfg["fact_dirs"]) if p.is_dir())
            if not dirs:
                raise SystemExit(
                    f"suite {suite}: no fact directories match {cfg['fact_dirs']!r} "
                    f"(is the input archive unpacked?)"
                )
            return [Benchmark(suite, p.name, file, p, egglog_timing) for p in dirs]
        return [Benchmark(suite, file.stem, file, egglog_timing=egglog_timing)]
    excluded = {f for pat in cfg.get("exclude", []) for f in d.glob(pat)}
    files = sorted(f for f in d.glob("*.egg") if f not in excluded)
    return [Benchmark(suite, f.stem, f, egglog_timing=egglog_timing) for f in files]


def resolve_benchmarks(specs: list[str]) -> list[Benchmark]:
    """Each spec is `suite`, `suite/name`, or `suite/glob*`."""
    out: list[Benchmark] = []
    for spec in specs:
        suite, _, pat = spec.partition("/")
        benches = load_suite(suite)
        if pat:
            benches = [b for b in benches if fnmatch.fnmatch(b.name, pat)]
            if not benches:
                raise SystemExit(f"no benchmark matches {spec!r}")
        out.extend(benches)
    return out


@dataclass(frozen=True)
class System:
    name: str
    # If set, a benchmark is supported only when `benchmarks/<suite>/<port_dir>/<name>.*`
    # (or `.../<port_dir>/<suite>.*` for suite-level programs) exists.
    port_dir: str | None = None
    notes: str = ""  # caveats about the system and its ports (from system.toml `notes`)

    @property
    def dir(self) -> Path:
        return SYSTEMS / self.name

    def supports(self, b: Benchmark) -> bool:
        """A port file with the benchmark's stem exists, or, for `base-<N>`, one with stem
        `base` (which the adapters run with --instance N)."""
        if self.port_dir is None:
            return True
        pd = b.dir / self.port_dir
        if not pd.is_dir():
            return False
        stems = {p.stem for p in pd.iterdir()}
        if b.name in stems or b.file.stem in stems:
            return True
        m = re.fullmatch(r"(.+)-(\d+)", b.name)
        return bool(m and m.group(1) in stems)


def load_system(name: str) -> System:
    d = SYSTEMS / name
    if not (d / "run").exists() or not (d / "build").exists():
        raise SystemExit(f"system {name}: expected executables {d}/build and {d}/run")
    cfg = {}
    if (d / "system.toml").exists():
        cfg = tomllib.loads((d / "system.toml").read_text())
    return System(name, cfg.get("port_dir"), cfg.get("notes", "").strip())


@dataclass(frozen=True)
class Variant:
    """One configuration of one system: a ref, CLI flags and run-time environment."""

    label: str
    system: str
    ref: str
    flags: tuple[str, ...] = ()
    patches: tuple[str, ...] = ()  # named system patches or explicit patch filenames
    threads: tuple[int, ...] | None = None  # overrides the experiment's thread list for this variant
    env: dict[str, str] = field(default_factory=dict)  # run-time overrides, recorded in result rows


@dataclass
class Experiment:
    name: str
    description: str
    benchmarks: list[str]
    variants: list[Variant]
    threads: list[int] = field(default_factory=lambda: [1])
    reps: int = 3
    timeout: float = 600.0  # seconds per run
    memory_gb: float = 32.0  # memory cap per run (cgroup via systemd-run, else RLIMIT_AS)
    cpus: str | None = None  # taskset CPU list, e.g. "0-63"
    plot: str = "bars"  # bars | scaling
    baseline: str | None = None  # variant label that speedups are relative to
    metric: str = "wall_s"  # default metric for summary/plot: wall_s or report.<field>
    speedup_labels: dict[str, str] = field(default_factory=dict)  # display names; result labels stay stable
    source_path: Path | None = None
    benchmark_timing: dict[str, str] = field(default_factory=dict)  # frozen per-benchmark egglog timing

    @property
    def results_dir(self) -> Path:
        return RESULTS / self.name

    def variant(self, label: str) -> Variant:
        for v in self.variants:
            if v.label == label:
                return v
        raise SystemExit(f"experiment {self.name}: no variant {label!r}")


def experiment_path(name: str | Path) -> Path:
    """Resolve a canonical experiment name or an explicit TOML filename."""
    p = Path(name).expanduser()
    if isinstance(name, Path) or p.suffix == ".toml" or p.parent != Path("."):
        return p.resolve()
    return EXPERIMENTS / f"{name}.toml"


def load_experiment(name: str | Path) -> Experiment:
    p = experiment_path(name)
    if not p.exists():
        raise SystemExit(f"no such experiment: {name} ({p})")
    cfg = tomllib.loads(p.read_text())
    return experiment_from_dict(p.stem, cfg, source_path=p)


def is_patch_path(patch: str) -> bool:
    path = Path(patch)
    return path.is_absolute() or path.suffix == ".patch" or path.parent != Path(".")


def patch_path(system: str, patch: str) -> Path:
    """Named patches live with adapters; explicit paths are already recipe-resolved."""
    if is_patch_path(patch):
        return Path(patch).expanduser().resolve()
    return SYSTEMS / system / "patches" / f"{patch}.patch"


def recipe_patch(patch: str, source_path: Path | None) -> str:
    if source_path is None or not is_patch_path(patch):
        return patch
    path = Path(patch).expanduser()
    if not path.is_absolute():
        path = source_path.parent / path
    return str(path.resolve())


def experiment_from_dict(name: str, cfg: dict, *, source_path: Path | None = None) -> Experiment:
    """Load either a recipe or the frozen effective config from a run bundle."""
    variants = [
        Variant(v["label"], v["system"], v["ref"], tuple(v.get("flags", [])),
                tuple(recipe_patch(p, source_path) for p in v.get("patches", [])),
                tuple(v["threads"]) if v.get("threads") is not None else None,
                validate_env(v.get("env", {}), v["label"]))
        for v in cfg["variants"]
    ]
    labels = [v.label for v in variants]
    if len(set(labels)) != len(labels):
        raise SystemExit(f"experiment {name}: duplicate variant labels")
    benchmark_timing = cfg.get("benchmark_timing", {})
    if not isinstance(benchmark_timing, dict) or any(
        not isinstance(benchmark, str) or not benchmark or scope not in ("process", "analysis")
        for benchmark, scope in benchmark_timing.items()
    ):
        raise SystemExit("benchmark_timing must map benchmark names to process or analysis")
    exp = Experiment(
        name=name,
        description=cfg.get("description", ""),
        benchmarks=cfg["benchmarks"],
        variants=variants,
        threads=cfg.get("threads", [1]),
        reps=cfg.get("reps", 3),
        timeout=cfg.get("timeout", 600.0),
        memory_gb=cfg.get("memory_gb", 32.0),
        cpus=cfg.get("cpus"),
        plot=cfg.get("plot", "bars"),
        baseline=cfg.get("baseline"),
        metric=cfg.get("metric", "wall_s"),
        speedup_labels=cfg.get("speedup_labels", {}),
        source_path=source_path,
        benchmark_timing=dict(benchmark_timing),
    )
    validate_settings(exp.threads, exp.reps, exp.timeout, exp.memory_gb)
    for variant in variants:
        if variant.threads is not None:
            validate_settings(variant.threads, exp.reps, exp.timeout, exp.memory_gb)
    if exp.baseline is not None:
        exp.variant(exp.baseline)
    return exp


def validate_env(env, label: str) -> dict[str, str]:
    if not isinstance(env, dict) or any(
        not isinstance(name, str) or not name or "=" in name or "\0" in name
        or not isinstance(value, str) or "\0" in value
        for name, value in env.items()
    ):
        raise SystemExit(f"variant {label!r}: env must map valid environment names to strings")
    return dict(env)


def validate_settings(threads, reps: int, timeout: float, memory_gb: float) -> None:
    if not threads or any(isinstance(t, bool) or not isinstance(t, int) or t <= 0 for t in threads):
        raise SystemExit("threads must be positive integers")
    if isinstance(reps, bool) or not isinstance(reps, int) or reps <= 0:
        raise SystemExit("reps must be a positive integer")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise SystemExit("timeout must be a positive finite number")
    if not isinstance(memory_gb, (int, float)) or not math.isfinite(memory_gb) or memory_gb < 0:
        raise SystemExit("memory_gb must be a nonnegative finite number")


def list_experiments() -> list[str]:
    return sorted(p.stem for p in EXPERIMENTS.glob("*.toml"))
