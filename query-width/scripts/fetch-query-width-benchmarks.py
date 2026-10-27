#!/usr/bin/env python3
"""Restore the pinned JOB/LSQB source snapshots, checking every SHA-256.

Usage: python scripts/fetch-query-width-benchmarks.py [--verify-only]
No database contents are downloaded. Revisions and file lists live in the
checked-in benchmarks/{job,lsqb}/manifest.json files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append", choices=("job", "lsqb"))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    for suite in args.suite or ("job", "lsqb"):
        directory = ROOT / "benchmarks" / suite
        manifest = json.loads((directory / "manifest.json").read_text())
        for entry in manifest["files"]:
            path = directory / entry["path"]
            if path.is_file():
                if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                    parser.error(f"modified source: {path}; move it aside before restoring the snapshot")
                continue
            if args.verify_only:
                parser.error(f"missing source: {path}")
            request = Request(entry["url"], headers={"User-Agent": "egglog-benchmarks"})
            with urlopen(request, timeout=30) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                parser.error(f"download checksum mismatch: {entry['url']}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        print(f"{suite}: verified {len(manifest['files'])} files at {manifest['commit']}")


if __name__ == "__main__":
    main()
