#!/usr/bin/env python3
"""Download the datasets of suite.toml from Hugging Face into facts/<dataset>/ and add a
TSV copy next to every CSV (egglog's `input` reads tab-separated files; FlowLog and Souffle
read the CSVs). Zips are cached in cache/. Usage: fetch.py [dataset ...] (default: all pairs).
"""

import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL = "https://huggingface.co/datasets/NemoYuu/flowlog_benchmark/resolve/main/dataset/csv/{}.zip"


def fetch(name: str, facts: Path, cache: Path) -> None:
    dest = facts / name
    if (dest / ".ready").exists():
        print(f"{name}: ready")
        return
    zpath = cache / f"{name}.zip"
    if not zpath.exists():
        print(f"{name}: downloading")
        tmp = zpath.with_suffix(".part")
        urllib.request.urlretrieve(URL.format(name), tmp)
        tmp.rename(zpath)
    print(f"{name}: extracting")
    with zipfile.ZipFile(zpath) as z:
        members = z.namelist()
        # zips unpack to <name>/<file>; tolerate a flat layout too
        if all(m.startswith(name + "/") for m in members):
            z.extractall(facts)
        else:
            z.extractall(dest)
    n = 0
    for csv in sorted(dest.glob("*.csv")):
        tsv = csv.with_suffix(".tsv")
        with open(csv, "rb") as fi, open(tsv, "wb") as fo:
            for line in fi:
                fo.write(line.replace(b",", b"\t"))
        n += 1
    (dest / ".ready").write_text("")
    print(f"{name}: {n} csv files converted to tsv")


def main() -> None:
    cfg = tomllib.loads((HERE / "suite.toml").read_text())
    facts = HERE / cfg.get("facts_root", "facts")
    cache = HERE / "cache"
    facts.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    wanted = sys.argv[1:] or sorted({p["facts"] for p in cfg["pairs"]})
    for name in wanted:
        fetch(name, facts, cache)


if __name__ == "__main__":
    main()
