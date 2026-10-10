#!/usr/bin/env python3
"""Generic build adapter for container-based systems (see systems/_container/README.md).

Usage: build <ref>      -> prints build/<system>/<commit12>
Invoked through the symlink systems/<system>/build, so the system name is the symlink's
directory. Resolves <ref> in a local clone of the baseline's repository, stages the
build context and runs `podman build`.
"""

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def sh(*cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def main() -> int:
    ref = sys.argv[1]
    # argv[0] is systems/<name>/build (a symlink to this file); its parent is the system directory
    sysdir = Path(sys.argv[0]).parent.resolve()
    name = sysdir.name
    root = sysdir.parent.parent
    cfg = tomllib.loads((sysdir / "system.toml").read_text())
    repo_url = cfg["repo"]

    repo = root / "build" / name / "repo"
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        sh("git", "clone", "-q", repo_url, str(repo), stdout=sys.stderr)
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sh("git", "-C", str(repo), "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*",
           "+refs/tags/*:refs/tags/*", stdout=sys.stderr)
        r = sh("git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}", capture_output=True)
    sha = r.stdout.strip()
    out = root / "build" / name / sha[:12]
    image = f"egglog-bench/{name}:{sha[:12]}"

    # stage the build context: system files + this system's ports
    ctx = out / "context"
    if ctx.exists():
        shutil.rmtree(ctx)
    shutil.copytree(sysdir, ctx / "system", symlinks=False)
    ports = []
    for suite_dir in sorted((root / "benchmarks").iterdir()):
        pd = suite_dir / cfg.get("port_dir", name)
        if pd.is_dir():
            shutil.copytree(pd, ctx / "ports" / suite_dir.name, symlinks=False)
            ports.append(suite_dir.name)

    print(f"building {image} from {name}@{ref} ({sha})", file=sys.stderr)
    sh("podman", "build", "-q", "--build-arg", f"REF={sha}", "-t", image,
       "-f", str(ctx / "system" / "Containerfile"), str(ctx), stdout=sys.stderr)

    (out / "build.json").write_text(json.dumps({
        "system": name, "ref": ref, "commit": sha, "image": image,
        "ports": ports, "threads": False, "flags": cfg.get("flags", []),
    }, indent=1))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
