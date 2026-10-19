"""Local, portable benchmark runs. Only explicitly selected results belong in Git."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from . import config as settings


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def file_identity(path: Path) -> dict:
    """Content identity, streamed so input datasets need not fit in memory."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


def tree_identity(path: Path) -> dict:
    """Hash file contents and relative paths; ignore timestamps and permissions."""
    path = Path(path)
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")
    digest = hashlib.sha256()
    size = count = 0
    for entry in sorted(path.rglob("*")):
        if entry.is_symlink() and entry.is_dir():
            raise ValueError(f"directory symlinks cannot be fingerprinted: {entry}")
        if entry.is_file():
            identity = file_identity(entry)
            digest.update(json.dumps([entry.relative_to(path).as_posix(), identity], sort_keys=True).encode())
            digest.update(b"\n")
            size += identity["size"]
            count += 1
    return {"sha256": digest.hexdigest(), "size": size, "files": count}


def write_manifest(rd: Path, manifest: dict) -> None:
    """Replace the manifest atomically, leaving no partial JSON for readers."""
    rd = Path(rd)
    with tempfile.NamedTemporaryFile("w", dir=rd, prefix=".manifest-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(rd / "run.json")
    finally:
        temporary.unlink(missing_ok=True)


def read_manifest(rd: Path) -> dict:
    try:
        manifest = json.loads((Path(rd) / "run.json").read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read run manifest in {rd}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError(f"unsupported run manifest in {rd}")
    for field in ("run_id", "experiment", "kind", "state", "machine_id"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise ValueError(f"missing or invalid {field} in {rd}/run.json")
    return manifest


def snapshot_files(rd: Path, mapping: dict[str, Path]) -> dict:
    """Copy explicitly named source files; never sweep arbitrary output trees."""
    snapshots = {}
    for label, source in mapping.items():
        source = Path(source)
        if not source.is_file():
            raise ValueError(f"snapshot source is not a file: {source}")
        if source.stat().st_size > 16 * 1024 * 1024:
            raise ValueError(f"source snapshot exceeds 16 MiB: {source}")
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip(".-")[:80] or "source"
        suffix = hashlib.sha256(label.encode()).hexdigest()[:12]
        destination = Path(rd) / "sources" / f"{slug}-{suffix}" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        snapshots[label] = {"path": destination.relative_to(rd).as_posix(), **file_identity(destination)}
    return snapshots


def _command(argv: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=10)
        return proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _provenance(rd: Path, source: Path | None) -> dict:
    root = settings.ROOT
    scopes = ["harness", "systems", "experiments", "tests", "pyproject.toml", "uv.lock"]
    commit = _command(["git", "rev-parse", "HEAD"], root).strip()
    diff = _command(["git", "diff", "--binary", "HEAD", "--", *scopes], root)
    untracked = _command(["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *scopes], root)
    sources = {}
    for name in filter(None, untracked.split("\0")):
        candidate = root / name
        # Capture source, not local diagnostics, credentials, caches, or datasets.
        if candidate.suffix in {".py", ".toml", ".lock", ".patch", ".rs", ".sh"} or candidate.name in {"build", "run"}:
            sources[f"untracked/{name}"] = candidate
    if source is not None:
        sources["experiment-config"] = Path(source)
    snapshots = snapshot_files(rd, sources)
    result = {
        "harness_commit": commit,
        "dirty": bool(diff or untracked),
        "sources": snapshots,
        "toolchain": {"python": platform.python_version()},
    }
    for command in ("rustc", "cargo"):
        version = _command([command, "--version"], root).strip()
        if version:
            result["toolchain"][command] = version
    if diff:
        patch = rd / "sources" / "working-tree.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text(diff)
        result["dirty_patch"] = {"path": patch.relative_to(rd).as_posix(), **file_identity(patch)}
    return result


def create_run(experiment: str, kind: str, config: dict, host: dict, *,
               machine: str | None = None, tag: str | None = None,
               source: Path | None = None) -> tuple[Path, dict]:
    if tag and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("run tags may contain letters, digits, '.', '_' and '-'")
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:12]
    if tag:
        run_id += "-" + tag
    rd = settings.RUNS / run_id
    rd.mkdir(parents=True, exist_ok=False)
    now = _now()
    manifest = {
        "schema_version": 1, "run_id": run_id, "experiment": experiment, "kind": kind,
        "state": "running", "created_at": now, "updated_at": now,
        "machine_id": machine or host.get("hostname") or platform.node() or "unknown",
        "host": host, "config": config,
    }
    write_manifest(rd, manifest)
    try:
        manifest["provenance"] = _provenance(rd, source)
        write_manifest(rd, manifest)
    except BaseException as exc:
        finish_run(rd, manifest, "failed", str(exc))
        raise
    return rd, manifest


def _payload(rd: Path) -> dict:
    if rd.is_symlink():
        raise ValueError(f"run directory cannot be a symlink: {rd}")
    files = {}
    for path in sorted(rd.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"run bundles cannot contain symlinks: {path}")
        if path.is_file() and path != rd / "run.json":
            files[path.relative_to(rd).as_posix()] = file_identity(path)
        elif not path.is_dir() and not path.is_file():
            raise ValueError(f"run bundles cannot contain special files: {path}")
    return files


def seal_run(rd: Path, manifest: dict) -> dict:
    """Record payload integrity without changing lifecycle state (also for selections)."""
    manifest["payload_checksums"] = _payload(Path(rd))
    write_manifest(rd, manifest)
    return manifest


def finish_run(rd: Path, manifest: dict, state: str, error: str | None = None) -> dict:
    if state not in {"complete", "failed", "interrupted"}:
        raise ValueError(f"invalid final run state: {state}")
    manifest.update(state=state, updated_at=_now())
    if error is not None:
        manifest["error"] = str(error)
    return seal_run(rd, manifest)


def verify_run(rd: Path) -> dict:
    manifest = read_manifest(rd)
    expected = manifest.get("payload_checksums")
    if not isinstance(expected, dict):
        raise ValueError(f"run has not been sealed: {rd}")
    actual = _payload(Path(rd))
    if expected != actual:
        changed = sorted(name for name in set(expected) | set(actual) if expected.get(name) != actual.get(name))
        raise ValueError(f"run payload checksum mismatch in {rd}: {', '.join(changed[:8])}")
    return manifest


def list_runs(experiment: str | None = None) -> list[Path]:
    if not settings.RUNS.exists():
        return []
    runs = []
    for rd in sorted(settings.RUNS.iterdir()):
        if rd.name.startswith(".") or rd.is_symlink() or not (rd / "run.json").is_file():
            continue
        if experiment is None or read_manifest(rd)["experiment"] == experiment:
            runs.append(rd)
    return runs


def resolve_run(id: str, experiment: str | None = None) -> Path:
    runs = list_runs(experiment)
    exact = [rd for rd in runs if rd.name == id]
    matches = exact or [rd for rd in runs if rd.name.startswith(id) or rd.name.endswith("-" + id)]
    if not matches:
        raise ValueError(f"no run matches {id!r}")
    if len(matches) != 1:
        raise ValueError(f"ambiguous run {id!r}: " + ", ".join(rd.name for rd in matches))
    return matches[0]


def export_run(id: str, out: Path) -> Path:
    rd = resolve_run(id)
    manifest = verify_run(rd)
    if manifest["state"] == "running":
        raise ValueError("cannot export a running run")
    out = Path(out)
    if out.is_dir():
        out /= rd.name + ".tar.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.resolve().is_relative_to(rd.resolve()):
        raise ValueError("export destination cannot be inside the run")
    with tempfile.NamedTemporaryFile(dir=out.parent, prefix=".export-", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            archive.add(rd, arcname=rd.name)
        # Detect a writer changing payload while the archive was built.
        verify_run(rd)
        temporary.replace(out)
    finally:
        temporary.unlink(missing_ok=True)
    return out


def import_run(archive: Path) -> Path:
    """Extract only ordinary files/directories, validate, then atomically install."""
    settings.RUNS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.RUNS, prefix=".import-") as temporary:
        staging = Path(temporary)
        try:
            with tarfile.open(archive, "r:*") as packed:
                members = packed.getmembers()
                roots = set()
                seen = set()
                for member in members:
                    name = PurePosixPath(member.name)
                    if (not name.parts or name.is_absolute() or ".." in name.parts or "\\" in member.name
                            or not (member.isfile() or member.isdir())):
                        raise ValueError(f"unsafe archive entry: {member.name}")
                    if name in seen:
                        raise ValueError(f"duplicate archive entry: {member.name}")
                    seen.add(name)
                    roots.add(name.parts[0])
                if len(roots) != 1:
                    raise ValueError("archive must contain exactly one run directory")
                run_id = roots.pop()
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
                    raise ValueError("invalid run directory name")
                for member in members:
                    destination = staging.joinpath(*PurePosixPath(member.name).parts)
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        source = packed.extractfile(member)
                        if source is None:
                            raise ValueError(f"cannot read archive entry: {member.name}")
                        with source, destination.open("xb") as target:
                            shutil.copyfileobj(source, target)
        except (tarfile.TarError, OSError) as exc:
            raise ValueError(f"cannot import {archive}: {exc}") from exc
        rd = staging / run_id
        manifest = verify_run(rd)
        if manifest["run_id"] != run_id or manifest["state"] == "running":
            raise ValueError("archive run ID/state does not match a finalized bundle")
        destination = settings.RUNS / run_id
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError(f"conflicting existing run: {run_id}")
            verify_run(destination)
            if tree_identity(destination) != tree_identity(rd):
                raise ValueError(f"conflicting existing run: {run_id}")
            return destination
        rd.rename(destination)
        return destination
