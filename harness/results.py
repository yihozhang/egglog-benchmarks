"""Read isolated runs and freeze explicit, portable result selections."""

from __future__ import annotations

import datetime as dt
import json
import math
import shutil
import statistics
import tempfile
from pathlib import Path

from . import artifacts, config
from .config import Experiment


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read {path}: {e}") from e


def _rows(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read measurements {path}: {e}") from e


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _safe_name(name: str) -> str:
    if not name or name in (".", "..") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in name):
        raise SystemExit(f"invalid name {name!r}: use letters, digits, dots, underscores or hyphens")
    return name


def run_files(exp: Experiment) -> list[Path]:
    """Legacy, committed JSONL runs (new runs live under runs/)."""
    return sorted(exp.results_dir.glob("*.jsonl"))


def _candidates(name: str) -> list[tuple[str, Path, dict | None]]:
    bundles = [(m["run_id"], rd, m) for rd in artifacts.list_runs(name)
               for m in [artifacts.read_manifest(rd)] if m.get("kind") == "matrix"]
    legacy = [(p.stem, p, None) for p in sorted((config.RESULTS / name).glob("*.jsonl"))]
    return sorted(bundles + legacy, key=lambda item: item[0])


def _choose(name: str, run: str) -> list[tuple[str, Path, dict | None]]:
    candidates = _candidates(name)
    if run in ("all", "latest"):
        candidates = [c for c in candidates if c[2] is not None and c[2].get("state") == "complete"]
        chosen = candidates[-1:] if run == "latest" else candidates
    else:
        exact = [c for c in candidates if c[0] == run]
        chosen = exact or [c for c in candidates if c[0].startswith(run) or c[0].endswith("-" + run)]
        if len(chosen) > 1:
            raise SystemExit(f"ambiguous run {run!r} for {name}; use an exact run ID")
    if not chosen:
        hint = "; legacy completion is unknown, so use --run <exact-id>" if run in ("latest", "all") else ""
        raise SystemExit(f"no {'complete ' if run in ('latest', 'all') else ''}run of {name} matches {run!r}{hint}")
    return chosen


def _load_candidate(item) -> list[dict]:
    _, path, manifest = item
    if manifest is None:
        return _rows(path)
    if manifest.get("state") != "complete":
        raise SystemExit(f"run {manifest['run_id']} is {manifest.get('state')}, not complete; use bench inspect")
    artifacts.verify_run(path)
    rows = _rows(path / "measurements.jsonl")
    if any(r.get("run_id") != manifest["run_id"] or r.get("experiment") != manifest["experiment"] for r in rows):
        raise SystemExit(f"measurements do not belong to run {manifest['run_id']}")
    validate_complete(manifest, rows)
    return rows


def load_rows(exp: Experiment, run: str = "latest", *, selection: str | None = None) -> list[dict]:
    if selection is not None:
        manifest, directory = read_selection(exp.name, selection)
        if manifest["kind"] != "matrix":
            raise SystemExit("query selections must be read with the query harness")
        rows = _rows(directory / "measurements.jsonl")
    else:
        rows = [r for item in _choose(exp.name, run) for r in _load_candidate(item)]
    validate_rows(rows)
    return rows



def report_data(experiment: str, run: str = "latest") -> tuple[Experiment, list[dict]]:
    """Modern reports use the captured recipe even after scratch sources disappear."""
    name = config.experiment_path(experiment).stem
    candidates = _choose(name, run)
    rows = [r for item in candidates for r in _load_candidate(item)]
    validate_rows(rows)
    manifests = [c[2] for c in candidates if c[2] is not None]
    if manifests:
        exp = config.experiment_from_dict(name, manifests[0]["config"])
        exp = config.experiment_from_dict(name, _selected_config(exp, manifests, rows))
    else:
        exp = config.load_experiment(experiment)
    return exp, rows


def key(row: dict) -> tuple:
    return (row["suite"], row["benchmark"], row["variant"], row["threads"])


def validate_rows(rows: list[dict]) -> None:
    """Never silently combine machines, code revisions, or overlapping run groups."""
    if not rows:
        raise SystemExit("no measurements in the selected runs")
    modern = [bool(r.get("environment_id")) for r in rows]
    if any(modern) and not all(modern):
        raise SystemExit("cannot combine legacy rows with provenance-aware runs; select them separately")
    if all(modern):
        environments = {r["environment_id"] for r in rows}
        machines = {r.get("machine_id") for r in rows}
        if len(environments) != 1 or len(machines) != 1:
            raise SystemExit("incompatible machines or measurement settings; make a separate selection for each environment")
    else:
        hosts = {_canonical(r.get("host", {})) for r in rows}
        if len(hosts) != 1:
            raise SystemExit("legacy runs come from different hosts; select each machine separately")
        if len({r.get("harness_commit") for r in rows}) != 1:
            raise SystemExit("legacy runs use different harness commits; select them separately")
    seen: set[tuple] = set()
    owners: dict[tuple, str] = {}
    configurations: dict[tuple, str] = {}
    builds: dict[str, str] = {}
    inputs: dict[tuple, str] = {}
    ports: dict[tuple, str] = {}
    for row in rows:
        k = key(row)
        run_id = row.get("run_id", "")
        if not run_id:
            raise SystemExit("measurement is missing run_id")
        if k in owners and owners[k] != run_id:
            raise SystemExit(f"overlapping runs for {'/'.join(map(str, k))}; select one complete repetition group, not latest-per-cell")
        owners[k] = run_id
        configuration = row.get("config_id")
        if configuration:
            if k in configurations and configurations[k] != configuration:
                raise SystemExit(f"configuration changed between repetitions for {k}")
            configurations[k] = configuration
        trial = (*k, row["rep"])
        if trial in seen:
            raise SystemExit(f"duplicate measurement for {k}, repetition {row['rep']}")
        seen.add(trial)
        identity = row.get("build_id") or _canonical({f: row.get(f) for f in ("system", "commit", "flags", "patches")})
        label = row["variant"]
        if label in builds and builds[label] != identity:
            raise SystemExit(f"variant {label!r} refers to different code, flags or patches across runs")
        builds[label] = identity
        port_id = row.get("port_id")
        if port_id:
            port_key = (*k[:2], row["variant"])
            if port_key in ports and ports[port_key] != port_id:
                raise SystemExit(f"different port sources or binaries for {port_key}")
            ports[port_key] = port_id
        input_id = row.get("input_id")
        bench = k[:2]
        if input_id:
            if bench in inputs and inputs[bench] != input_id:
                raise SystemExit(f"different benchmark inputs for {'/'.join(bench)}")
            inputs[bench] = input_id


def validate_complete(manifest: dict, rows: list[dict]) -> None:
    cases = manifest.get("cases")
    if cases is None:
        raise SystemExit(f"run {manifest['run_id']} has no planned case inventory")
    planned = {key(c): c["reps"] for c in cases}
    if len(planned) != len(cases):
        raise SystemExit("run plan contains duplicate configurations")
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
        for field in ("environment_id", "machine_id"):
            if row.get(field) != manifest.get(field):
                raise SystemExit(f"measurement {field} disagrees with its run manifest")
    missing, extra = planned.keys() - groups.keys(), groups.keys() - planned.keys()
    if missing or extra:
        raise SystemExit(f"run {manifest['run_id']} has missing or unexpected configurations: missing={len(missing)}, extra={len(extra)}")
    for k, group in groups.items():
        group = sorted(group, key=lambda r: r["rep"])
        reps = [r["rep"] for r in group]
        if reps != list(range(len(group))) or len(group) > planned[k]:
            raise SystemExit(f"missing or duplicate repetitions for {k}")
        if any(r["status"] != "ok" for r in group[:-1]):
            raise SystemExit(f"measurements continue after a terminal failure for {k}")
        if len(group) != planned[k] and group[-1]["status"] == "ok":
            raise SystemExit(f"incomplete repetitions for {k}: {len(group)}/{planned[k]}")


def summarize(rows: list[dict], metric: str = "wall_s") -> dict[tuple, dict]:
    """Median over complete, explicitly chosen repetition groups."""
    validate_rows(rows)
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    out = {}
    for k, rs in groups.items():
        ok = [r for r in rs if r["status"] == "ok"]
        statuses = {r["status"] for r in rs}
        status = next((st for st in ("timeout", "oom", "unsupported", "fail") if st in statuses), "ok" if ok else "fail")
        vals = [v for r in ok if (v := value(r, metric)) is not None]
        out[k] = {
            "status": status, "n": len(vals),
            "median": statistics.median(vals) if vals else None,
            "min": min(vals) if vals else None, "max": max(vals) if vals else None,
            "sizes": ok[0].get("report", {}).get("sizes") if ok else None,
            "run_id": rs[0]["run_id"],
        }
    return out


def _checksums(directory: Path) -> dict:
    return {p.relative_to(directory).as_posix(): artifacts.file_identity(p)
            for p in sorted(directory.rglob("*")) if p.is_file() and p != directory / "selection.json"}


def read_selection(experiment: str, name: str) -> tuple[dict, Path]:
    directory = config.RESULTS / _safe_name(experiment) / _safe_name(name)
    manifest = _json(directory / "selection.json")
    if manifest.get("schema_version") != 1 or manifest.get("experiment") != experiment or manifest.get("name") != name:
        raise SystemExit(f"invalid selection manifest in {directory}")
    if not manifest.get("checksums") or _checksums(directory) != manifest["checksums"]:
        raise SystemExit(f"selection {name!r} is missing files or has modified content")
    return manifest, directory


def load_query_selection(experiment: str, name: str) -> Path:
    manifest, directory = read_selection(experiment, name)
    if manifest["kind"] != "query" or len(manifest["run_ids"]) != 1:
        raise SystemExit("expected a selection containing one query run")
    return directory / "runs" / manifest["run_ids"][0]


def _compact_bundle(source: Path, target: Path) -> None:
    shutil.copytree(source, target, ignore=lambda _d, names: [n for n in names if n == "logs"])
    manifest = artifacts.read_manifest(target)
    manifest["archive_scope"] = "measurements-and-provenance; raw logs omitted"
    artifacts.seal_run(target, manifest)


def _selected_config(exp: Experiment, manifests: list[dict], rows: list[dict]) -> dict:
    from dataclasses import asdict
    cfg = dict(manifests[0]["config"]) if manifests else asdict(exp)
    cfg.pop("source_path", None)
    cfg.pop("name", None)
    variants = {}
    for m in manifests:
        for v in m["config"].get("variants", []):
            variants.setdefault(v["label"], v)
    if not variants:
        variants = {v.label: asdict(v) for v in exp.variants}
    labels = {r["variant"] for r in rows}
    cfg["variants"] = [v for label, v in variants.items() if label in labels]
    cfg["benchmarks"] = sorted({f"{r['suite']}/{r['benchmark']}" for r in rows})
    cfg["threads"] = sorted({r["threads"] for r in rows})
    for v in cfg["variants"]:
        v["threads"] = sorted({r["threads"] for r in rows if r["variant"] == v["label"]})
    if cfg.get("baseline") not in labels:
        cfg["baseline"] = None
    return cfg


def select_runs(experiment: str, runs: list[str], name: str, *, allow_legacy: bool = False, metric: str | None = None) -> Path:
    """Copy reviewable measurements + provenance into results/, without committing."""
    from . import query
    path = config.experiment_path(experiment)
    exp_name = path.stem
    target = config.RESULTS / _safe_name(exp_name) / _safe_name(name)
    if target.exists():
        raise SystemExit(f"selection already exists: {target}; choose a new version name")
    if len(runs) != len(set(runs)):
        raise SystemExit("the same run was selected more than once")
    if not runs:
        raise SystemExit("select requires at least one run")
    if any(run in ("all", "latest") for run in runs):
        raise SystemExit("select requires exact run IDs or unique prefixes/tags, not 'all' or 'latest'")
    first_bundle = None
    try:
        first_bundle = artifacts.read_manifest(artifacts.resolve_run(runs[0], exp_name))
    except ValueError:
        pass
    is_query = first_bundle.get("kind") == "query" if first_bundle else query.is_query(experiment)
    if is_query:
        if len(runs) != 1:
            raise SystemExit("query selections currently require exactly one complete run")
        rd = query.run_dir(experiment, runs[0])
        manifest = _json(rd / "run.json")
        legacy = manifest.get("schema_version") != 1
        if legacy and not allow_legacy:
            raise SystemExit("legacy query run has incomplete provenance; use --allow-legacy to record this explicitly")
        if not legacy:
            if manifest.get("state") != "complete":
                raise SystemExit("cannot select an incomplete query run")
            artifacts.verify_run(rd)
        candidates = [(manifest["run_id"], rd, None if legacy else manifest)]
        rows, manifests, cfg = [], [manifest], manifest["config"]
        kind = "query"
    else:
        exp = (config.experiment_from_dict(exp_name, first_bundle["config"])
               if first_bundle else config.load_experiment(experiment))
        candidates = [_choose(exp.name, run)[0] for run in runs if run not in ("all", "latest")]
        if len(candidates) != len(runs):
            raise SystemExit("select requires exact run IDs or unique prefixes/tags, not 'all' or 'latest'")
        if len({c[0] for c in candidates}) != len(candidates):
            raise SystemExit("multiple selectors refer to the same run")
        legacy = any(c[2] is None for c in candidates)
        if legacy and not allow_legacy:
            raise SystemExit("legacy JSONL has incomplete provenance; use --allow-legacy to record this explicitly")
        rows = [r for c in candidates for r in _load_candidate(c)]
        validate_rows(rows)
        manifests = [c[2] for c in candidates if c[2] is not None]
        cfg = _selected_config(exp, manifests, rows)
        kind = "matrix"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".select-", dir=target.parent))
    try:
        for run_id, source, manifest in candidates:
            dest = temporary / "runs" / run_id
            dest.parent.mkdir(parents=True, exist_ok=True)
            if manifest is not None:
                _compact_bundle(source, dest)
            elif source.is_file():
                dest.mkdir()
                shutil.copy2(source, dest / "measurements.jsonl")
            else:
                shutil.copytree(source, dest)
        if kind == "matrix":
            (temporary / "measurements.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        selection = {
            "schema_version": 1, "experiment": exp_name, "name": name, "kind": kind,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "run_ids": [c[0] for c in candidates], "config": cfg,
            "metric": metric or cfg.get("metric", "wall_s"),
            "analysis": "median of successful repetitions; groups with any failure are excluded from speedups; all outcomes retained; no latest-per-cell replacement",
            "provenance": "legacy-incomplete" if legacy else "captured",
            "coverage": [dict(zip(("suite", "benchmark", "variant", "threads"), k)) for k in sorted({key(r) for r in rows})],
            "checksums": _checksums(temporary),
        }
        (temporary / "selection.json").write_text(json.dumps(selection, indent=2, sort_keys=True) + "\n")
        temporary.rename(target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def value(row: dict, metric: str):
    if metric.startswith("report."):
        return row.get("report", {}).get(metric[len("report."):])
    return row.get(metric)


def geomean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None
