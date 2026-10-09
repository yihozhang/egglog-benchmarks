"""Loading result rows and reducing them to one number per configuration."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from .config import Experiment


def run_files(exp: Experiment) -> list[Path]:
    return sorted(exp.results_dir.glob("*.jsonl"))


def load_rows(exp: Experiment, run: str = "latest") -> list[dict]:
    """`run` is 'latest', 'all', or a run id (prefix match)."""
    files = run_files(exp)
    if not files:
        raise SystemExit(f"no results for experiment {exp.name}")
    if run == "latest":
        files = files[-1:]
    elif run != "all":
        # a run id prefix (timestamp) or a tag suffix
        files = [f for f in files if f.stem.startswith(run) or f.stem.endswith("-" + run)]
        if not files:
            raise SystemExit(f"no run of {exp.name} matches {run!r}")
    rows = []
    for f in files:
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def key(row: dict) -> tuple:
    return (row["suite"], row["benchmark"], row["variant"], row["threads"])


def summarize(rows: list[dict], metric: str = "wall_s") -> dict[tuple, dict]:
    """Median of `metric` over repetitions for every (suite, benchmark, variant, threads).

    If several runs are loaded, the latest run that has data for a key wins
    (rows are in file order, so later files overwrite earlier ones).
    """
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out = {}
    for k, rs in groups.items():
        latest = max(r["run_id"] for r in rs)
        rs = [r for r in rs if r["run_id"] == latest]
        ok = [r for r in rs if r["status"] == "ok"]
        statuses = {r["status"] for r in rs}
        status = "ok" if ok else next(
            (st for st in ("timeout", "oom", "unsupported", "fail") if st in statuses), "fail")
        vals = [value(r, metric) for r in ok]
        vals = [v for v in vals if v is not None]
        out[k] = {
            "status": status,
            "n": len(vals),
            "median": statistics.median(vals) if vals else None,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
            "sizes": ok[0].get("report", {}).get("sizes") if ok else None,
            "run_id": latest,
        }
    return out


def value(row: dict, metric: str):
    """`wall_s` or `report.<field>`."""
    if metric.startswith("report."):
        return row.get("report", {}).get(metric[len("report."):])
    return row.get(metric)


def geomean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None and x > 0]
    if not xs:
        return None
    return math.exp(sum(math.log(x) for x in xs) / len(xs))
