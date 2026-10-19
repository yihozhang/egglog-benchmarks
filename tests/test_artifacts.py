"""Portable run integrity and safe, deterministic import behavior."""

import io
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import artifacts, config


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(config, "RUNS", self.root / "runs", create=True).start()
        patch.object(artifacts, "_provenance", return_value={"harness_commit": "test"}).start()

    def make_run(self, tag=None):
        rd, manifest = artifacts.create_run("e2e", "matrix", {"reps": 1}, {"hostname": "server"}, tag=tag)
        (rd / "samples.jsonl").write_text('{"wall_s": 1.5}\n')
        (rd / "logs").mkdir()
        (rd / "logs" / "stdout").write_text("completed\n")
        artifacts.finish_run(rd, manifest, "complete")
        return rd, manifest

    def test_unique_ids_and_resolution(self):
        first, _ = self.make_run("trial")
        second, _ = self.make_run("trial")
        self.assertNotEqual(first, second)
        self.assertEqual(artifacts.resolve_run(first.name), first)
        self.assertEqual(len(artifacts.list_runs("e2e")), 2)
        self.assertEqual(artifacts.list_runs("other"), [])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            artifacts.resolve_run("trial")
        with self.assertRaises(ValueError):
            artifacts.create_run("e2e", "matrix", {}, {}, tag="../../outside")

    def test_export_import_round_trip_and_idempotency(self):
        source, original = self.make_run()
        archive = artifacts.export_run(source.name, self.root / "run.tar.gz")
        config.RUNS = self.root / "another-machine"
        restored = artifacts.import_run(archive)
        self.assertEqual(artifacts.verify_run(restored), original)
        self.assertEqual(artifacts.tree_identity(source), artifacts.tree_identity(restored))
        self.assertEqual(artifacts.import_run(archive), restored)
        self.assertEqual(artifacts.read_manifest(restored)["machine_id"], "server")

    def test_payload_tampering_is_rejected(self):
        rd, _ = self.make_run()
        (rd / "samples.jsonl").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            artifacts.verify_run(rd)
        with self.assertRaisesRegex(ValueError, "checksum"):
            artifacts.export_run(rd.name, self.root / "tampered.tar.gz")
        archive = self.root / "tampered-import.tar.gz"
        with tarfile.open(archive, "w:gz") as packed:
            packed.add(rd, arcname=rd.name)
        config.RUNS = self.root / "imported"
        with self.assertRaisesRegex(ValueError, "checksum"):
            artifacts.import_run(archive)
        self.assertEqual(list(config.RUNS.iterdir()), [])

    def test_duplicate_id_with_different_valid_contents_is_rejected(self):
        rd, manifest = self.make_run()
        archive = artifacts.export_run(rd.name, self.root / "original.tar.gz")
        (rd / "samples.jsonl").write_text('{"wall_s": 2.0}\n')
        artifacts.seal_run(rd, manifest)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            artifacts.import_run(archive)
        self.assertIn("2.0", (rd / "samples.jsonl").read_text())

    def test_unsafe_archive_paths_and_links_are_rejected(self):
        for name, link in [("../escaped", None), ("/absolute", None), ("run/../../escaped", None),
                           ("run/link", "/tmp/outside"), ("run/link", "../outside")]:
            with self.subTest(name=name, link=link):
                archive = self.root / "unsafe.tar"
                with tarfile.open(archive, "w") as packed:
                    entry = tarfile.TarInfo(name)
                    if link:
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = link
                        packed.addfile(entry)
                    else:
                        entry.size = 3
                        packed.addfile(entry, io.BytesIO(b"bad"))
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    artifacts.import_run(archive)
                self.assertFalse((self.root / "escaped").exists())

    def test_unlisted_file_and_symlink_in_bundle_are_rejected(self):
        rd, manifest = self.make_run()
        extra = rd / "extra"
        extra.write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "checksum"):
            artifacts.verify_run(rd)
        extra.unlink()
        extra.symlink_to(rd / "samples.jsonl")
        with self.assertRaisesRegex(ValueError, "symlink"):
            artifacts.seal_run(rd, manifest)

    def test_source_snapshots_and_tree_identity_include_names(self):
        rd, manifest = self.make_run()
        source = self.root / "experiment.toml"
        source.write_text('description = "test"\n')
        snapshots = artifacts.snapshot_files(rd, {"../../config": source})
        snapshot = rd / snapshots["../../config"]["path"]
        self.assertTrue(snapshot.is_relative_to(rd))
        self.assertEqual(snapshot.read_bytes(), source.read_bytes())
        before = artifacts.tree_identity(rd)
        snapshot.rename(snapshot.with_name("renamed.toml"))
        self.assertNotEqual(before["sha256"], artifacts.tree_identity(rd)["sha256"])

    def test_selected_benchmark_sources_survive_cleanup_and_transfer(self):
        rd, manifest = self.make_run()
        suite = self.root / "scratch-suite"
        (suite / "egg").mkdir(parents=True)
        sources = {
            "benchmarks/trial/program.egg": (suite / "program.egg", '(include "header.egg")\n'),
            "benchmarks/trial/header.egg": (suite / "header.egg", '(datatype Expr (Num i64))\n'),
            "benchmarks/trial/egg/program.rs": (suite / "egg" / "program.rs", 'fn main() {}\n'),
        }
        for source, content in sources.values():
            source.write_text(content)
        # Dataset contents are intentionally outside the explicit source inventory.
        (suite / "facts.csv").write_text("1,2,3\n")
        manifest["input_sources"] = artifacts.snapshot_files(rd, {label: value[0] for label, value in sources.items()})
        artifacts.seal_run(rd, manifest)
        shutil.rmtree(suite)
        archive = artifacts.export_run(rd.name, self.root / "source-snapshots.tar.gz")
        config.RUNS = self.root / "transferred"
        restored = artifacts.import_run(archive)
        transferred = artifacts.verify_run(restored)
        for label, identity in transferred["input_sources"].items():
            self.assertEqual((restored / identity["path"]).read_text(), sources[label][1])
        self.assertFalse(any(path.name == "facts.csv" for path in restored.rglob("*")))


class ProvenanceTests(unittest.TestCase):
    def test_dirty_tracked_and_untracked_source_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "harness").mkdir()
            tracked = root / "harness" / "example.py"
            tracked.write_text("value = 1\n\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "add", "harness/example.py"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Artifact Test", "-c", "user.email=test@example.invalid",
                            "commit", "-qm", "initial"], cwd=root, check=True)
            tracked.write_text("value = 2\n\n")
            untracked = root / "harness" / "new.py"
            untracked.write_text("another = 3\n")
            source = root / "local.toml"
            source.write_text('description = "trial"\n')
            # Unknown output files are deliberately not copied as source.
            (root / "harness" / "output.log").write_text("log output")
            rd = root / "run"
            rd.mkdir()
            command = artifacts._command
            def local_commands(argv, cwd):
                return "test toolchain" if argv[0] in {"rustc", "cargo"} else command(argv, cwd)
            with patch.object(config, "ROOT", root), patch.object(artifacts, "_command", side_effect=local_commands):
                provenance = artifacts._provenance(rd, source)
            self.assertTrue(provenance["dirty"])
            self.assertEqual(len(provenance["harness_commit"]), 40)
            diff = (rd / provenance["dirty_patch"]["path"]).read_text()
            self.assertIn("+value = 2", diff)
            subprocess.run(["git", "apply", "--reverse", "--check", str(rd / provenance["dirty_patch"]["path"])],
                           cwd=root, check=True)
            self.assertEqual(set(provenance["sources"]), {"untracked/harness/new.py", "experiment-config"})
            snapshot = provenance["sources"]["untracked/harness/new.py"]
            self.assertEqual((rd / snapshot["path"]).read_text(), untracked.read_text())


if __name__ == "__main__":
    unittest.main()
