"""Loading of suites, systems and experiments from the repository layout."""

from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCHMARKS = ROOT / "benchmarks"
SYSTEMS = ROOT / "systems"
EXPERIMENTS = ROOT / "experiments"
RESULTS = ROOT / "results"
FIGURES = ROOT / "figures"


@dataclass(frozen=True)
class Benchmark:
    suite: str
    name: str
    file: Path  # the egglog program
    fact_dir: Path | None = None  # directory for `input` commands, if any

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
    if "pairs" in cfg:
        root = d / cfg.get("facts_root", "facts")
        out = []
        for pair in cfg["pairs"]:
            file = d / pair["file"]
            if not file.exists():
                raise SystemExit(f"suite {suite}: {file} does not exist")
            out.append(Benchmark(suite, f"{file.stem}-{pair['facts']}", file, root / pair["facts"]))
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
            return [Benchmark(suite, p.name, file, p) for p in dirs]
        return [Benchmark(suite, file.stem, file)]
    excluded = {f for pat in cfg.get("exclude", []) for f in d.glob(pat)}
    files = sorted(f for f in d.glob("*.egg") if f not in excluded)
    return [Benchmark(suite, f.stem, f) for f in files]


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
    """One configuration of one system: a label, a git ref and extra CLI flags."""

    label: str
    system: str
    ref: str
    flags: tuple[str, ...] = ()
    patches: tuple[str, ...] = ()  # systems/<system>/patches/<name>.patch, applied at build time
    threads: tuple[int, ...] | None = None  # overrides the experiment's thread list for this variant


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

    @property
    def results_dir(self) -> Path:
        return RESULTS / self.name

    def variant(self, label: str) -> Variant:
        for v in self.variants:
            if v.label == label:
                return v
        raise SystemExit(f"experiment {self.name}: no variant {label!r}")


def load_experiment(name: str) -> Experiment:
    p = EXPERIMENTS / f"{name}.toml"
    if not p.exists():
        raise SystemExit(f"no such experiment: {name} ({p})")
    cfg = tomllib.loads(p.read_text())
    variants = [
        Variant(v["label"], v["system"], v["ref"], tuple(v.get("flags", [])), tuple(v.get("patches", [])),
                tuple(v["threads"]) if "threads" in v else None)
        for v in cfg["variants"]
    ]
    labels = [v.label for v in variants]
    if len(set(labels)) != len(labels):
        raise SystemExit(f"experiment {name}: duplicate variant labels")
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
    )
    if exp.baseline is not None:
        exp.variant(exp.baseline)
    return exp


def list_experiments() -> list[str]:
    return sorted(p.stem for p in EXPERIMENTS.glob("*.toml"))
