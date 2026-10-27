"""Query bundle lifecycle and selection tests without building benchmark engines."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import artifacts, config, query


class QueryArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runs = self.root / "runs"
        self.results = self.root / "results"
        self.qdir = self.root / "systems" / "egglog-query"
        self.qdir.mkdir(parents=True)
        for filename in ("build", "run_suite.py", "bench_db.py", "sqlgen.py"):
            (self.qdir / filename).write_text(f"# fixture {filename}\n")
        (self.qdir / "driver" / "src").mkdir(parents=True)
        (self.qdir / "driver" / "src" / "main.rs").write_text("fn main() {}\n")
        self.experiments = self.root / "experiments"
        self.experiments.mkdir()
        self.recipe = self.experiments / "queries.toml"
        self.recipe.write_text('kind = "query"\nsuite = "fixture"\negglog_ref = "main"\n'
                               'engines = ["duckdb"]\nthreads = 1\nreps = 3\ntimeout = 60\n')
        suite = self.root / "benchmarks" / "fixture"
        suite.mkdir(parents=True)
        for filename in ("a.egg", "b.egg", "header.egg"):
            (suite / filename).write_text("(relation R (i64))\n")
        self.commit = "1234567890abcdef" * 2 + "12345678"
        driver_root = self.root / "build" / "egglog-query" / self.commit[:12] / "driver"
        self.driver = driver_root / "target" / "release" / "egglog-query-driver"
        self.driver.parent.mkdir(parents=True)
        self.driver.write_bytes(b"driver fixture")
        (driver_root / "Cargo.toml").write_text('[package]\nname = "fixture"\n')
        (driver_root / "Cargo.lock").write_text("# locked dependencies\n")
        dependency_root = self.root / "build" / "egglog" / self.commit[:12]
        dependency_root.mkdir(parents=True)
        (dependency_root / "build.json").write_text(json.dumps({"commit": self.commit, "ref": "main"}))
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for owner, field, value in (
            (config, "ROOT", self.root), (config, "RUNS", self.runs),
            (config, "EXPERIMENTS", self.experiments), (config, "RESULTS", self.results),
            (query, "ROOT", self.root), (query, "RESULTS", self.results), (query, "QDIR", self.qdir),
        ):
            self.stack.enter_context(mock.patch.object(owner, field, value))
        self.stack.enter_context(mock.patch.object(artifacts, "_provenance", return_value={"harness_commit": "harness-fixture"}))
        self.stack.enter_context(mock.patch.object(query, "host_info", return_value={"hostname": "test-host"}))
        self.stack.enter_context(mock.patch.object(query, "_package_version", return_value="1.2.3"))
        self.calls = []
        self.fail_on = None
        self.failure = None
        self.empty_database_output = False
        self.process = self.stack.enter_context(mock.patch.object(query.subprocess, "run", side_effect=self.fake_process))
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def fake_process(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == str(self.qdir / "build"):
            phase = "build"
            result = subprocess.CompletedProcess(argv, 0, stdout=f"{self.driver}\n")
        elif argv[0] == "git":
            phase = "git"
            result = subprocess.CompletedProcess(argv, 0, stdout=f"{self.commit}\n")
        elif argv[1] == str(self.qdir / "run_suite.py"):
            phase = "suite"
            destination = Path(argv[3]) / argv[4]
            destination.mkdir(parents=True)
            self.write_program(destination)
            result = subprocess.CompletedProcess(argv, 0)
        elif argv[1] == str(self.qdir / "bench_db.py"):
            phase = "database"
            record = {"query": "r0", "status": "ok", "rows": 1,
                      "optimize_ms": 1, "execute_ms": 2, "operator_ms": 2}
            (Path(argv[3]) / f"{argv[2]}.jsonl").write_text("" if self.empty_database_output else json.dumps(record) + "\n")
            result = subprocess.CompletedProcess(argv, 0)
        else:
            raise AssertionError(f"unexpected subprocess {argv}")
        if self.fail_on == phase:
            raise self.failure
        return result

    @staticmethod
    def write_program(destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "index.json").write_text(json.dumps({"queries": [{"id": "r0", "name": "rule", "atoms": 1}]}))
        record = {"query": "r0", "query_s": 0.002, "other_s": 0.001, "size": 1}
        (destination / "egglog.jsonl").write_text("".join(json.dumps({**record, "no_decomp": mode}) + "\n"
                                                         for mode in (False, True)))

    def make_bundle(self, run_id, state="complete", created="2026-01-01", kind="query"):
        directory = self.runs / run_id
        directory.mkdir(parents=True)
        artifacts.write_manifest(directory, {
            "schema_version": 1, "run_id": run_id, "experiment": "queries", "kind": kind,
            "state": state, "created_at": created, "machine_id": "fixture", "provenance": {"known": True},
        })
        return directory

    def test_no_decomp_only_payload_rejects_decomposed_measurements(self):
        destination = self.root / "paper-payload"
        self.write_program(destination)
        with self.assertRaisesRegex(ValueError, "unexpected"):
            query._validate_program_payload(destination, [], no_decomp_only=True)
        path = destination / "egglog.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        record = next(r for r in records if r["no_decomp"])
        record["engine_label"] = "egglog-cracked"
        path.write_text(json.dumps(record) + "\n")
        query._validate_program_payload(destination, [], no_decomp_only=True)

    def test_run_routes_payloads_and_records_effective_options_and_provenance(self):
        directory = query.run(str(self.recipe), programs=["b"], threads=[4], reps=7, timeout=12.5,
                              machine="machine-a", tag="trial")
        self.assertEqual(directory.parent, self.runs)
        self.assertFalse(self.results.exists())
        manifest = artifacts.verify_run(directory)
        self.assertEqual(manifest["kind"], "query")
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(manifest["experiment"], "queries")
        self.assertEqual(manifest["machine_id"], "machine-a")
        self.assertEqual(manifest["config"]["threads"], 4)
        self.assertEqual(manifest["config"]["reps"], 7)
        self.assertEqual(manifest["config"]["timeout"], 12.5)
        self.assertEqual(manifest["config"]["programs"], ["b"])
        self.assertEqual(manifest["cli_overrides"]["threads"], [4])
        self.assertEqual(manifest["measurement"]["egglog"], {"threads": 1, "reps": 1, "timeout": None})
        self.assertEqual(manifest["builds"]["egglog"]["commit"], self.commit)
        self.assertEqual(manifest["builds"]["duckdb"]["version"], "1.2.3")
        self.assertIn("sha256", manifest["builds"]["egglog"]["driver"])
        self.assertIn("sha256", manifest["inputs"]["suite"])
        self.assertEqual(manifest["plan"], [{"program": "b", "engines": ["egglog", "egglog_nd", "duckdb"]}])
        self.assertIn("b/index.json", manifest["payload_checksums"])
        self.assertIn("b/duckdb.jsonl", manifest["payload_checksums"])
        database = next(argv for argv, _ in self.calls if len(argv) > 1 and argv[1] == str(self.qdir / "bench_db.py"))
        self.assertEqual(database[-6:], ["--threads", "4", "--reps", "7", "--timeout", "12.5"])
        suite_call = next((argv, kwargs) for argv, kwargs in self.calls if len(argv) > 1 and argv[1] == str(self.qdir / "run_suite.py"))
        self.assertEqual(Path(suite_call[0][3]), self.root / "build" / "query" / "queries" / directory.name)
        self.assertEqual(suite_call[1]["env"]["EGGLOG_QUERY_DRIVER"], str(self.driver))
        # Source snapshot directories are not mistaken for program result directories.
        self.assertEqual(len(query.load_rows(directory)), 1)

    def test_dry_run_does_not_build_or_create_a_bundle(self):
        result = query.run("queries", programs=["a"], threads=[2], reps=5, timeout=9, dry_run=True)
        self.assertIsNone(result)
        self.process.assert_not_called()
        self.assertFalse(self.runs.exists())
        self.assertIn('"reps": 5', self.output.getvalue())

    def test_rule_sources_and_recursive_includes_survive_original_removal(self):
        suite = self.root / "benchmarks" / "fixture"
        (suite / "rules").mkdir()
        (suite / "b.egg").write_text('(include "header.egg")\n(relation R (i64))\n')
        (suite / "header.egg").write_text('(include "rules/common.egg")\n')
        (suite / "rules" / "common.egg").write_text("(relation Common (i64))\n")
        directory = query.run("queries", programs=["b"])
        manifest = artifacts.verify_run(directory)
        snapshots = manifest["inputs"]["source_snapshots"]
        self.assertEqual({Path(label).name for label in snapshots}, {"b.egg", "header.egg", "common.egg"})
        contents = {Path(label).name: (directory / entry["path"]).read_text() for label, entry in snapshots.items()}
        self.assertEqual(contents["common.egg"], "(relation Common (i64))\n")
        self.recipe.unlink()
        for source in (suite / "b.egg", suite / "header.egg", suite / "rules" / "common.egg"):
            source.unlink()
        artifacts.verify_run(directory)
        self.assertEqual(query.run_dir("queries"), directory)
        self.assertEqual(len(query.load_rows(directory)), 1)
        recipe_snapshot = directory / manifest["query_sources"]["recipe"]["path"]
        self.assertIn('kind = "query"', recipe_snapshot.read_text())

    def test_resource_controls_and_unsupported_thread_settings_are_rejected(self):
        for kwargs, message in (
            ({"cpus": "0-3"}, "--cpus"), ({"memory_gb": 2}, "--memory-gb"),
            ({"threads": [1, 2]}, "exactly one"),
            ({"threads": [2], "engines": ["umbra"]}, "does not implement thread control"),
            ({"reps": 0}, "positive integer"), ({"timeout": float("inf")}, "positive and finite"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(SystemExit, message):
                query.run("queries", **kwargs)
        self.process.assert_not_called()
        self.assertFalse(self.runs.exists())

    def test_failed_build_is_a_failed_bundle(self):
        self.fail_on, self.failure = "build", subprocess.CalledProcessError(1, ["build"])
        with self.assertRaises(subprocess.CalledProcessError):
            query.run("queries", programs=["a"])
        directory, = artifacts.list_runs("queries")
        manifest = artifacts.verify_run(directory)
        self.assertEqual(manifest["state"], "failed")
        self.assertIn("error", manifest)
        self.assertIn("plan", manifest)

    def test_interrupt_preserves_finished_and_partial_program_payloads(self):
        self.fail_on, self.failure = "database", KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            query.run("queries", programs=["a"])
        directory, = artifacts.list_runs("queries")
        manifest = artifacts.verify_run(directory)
        self.assertEqual(manifest["state"], "interrupted")
        self.assertTrue((directory / "a" / "index.json").exists())
        self.assertTrue((directory / "a" / "egglog.jsonl").exists())
        self.assertTrue((directory / "a" / "duckdb.jsonl").exists())

    def test_successful_process_with_missing_measurements_is_failed(self):
        self.empty_database_output = True
        with self.assertRaisesRegex(ValueError, "missing 1 measurements"):
            query.run("queries", programs=["a"])
        directory, = artifacts.list_runs("queries")
        self.assertEqual(artifacts.verify_run(directory)["state"], "failed")

    def test_report_rejects_modified_sealed_payload(self):
        directory = query.run("queries", programs=["a"])
        (directory / "a" / "duckdb.jsonl").write_text("")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            query.summary("queries")

    def test_latest_excludes_incomplete_and_non_query_bundles(self):
        complete = self.make_bundle("complete", created="2026-01-01")
        self.make_bundle("interrupted", "interrupted", "2026-02-01")
        self.make_bundle("running", "running", "2026-03-01")
        self.make_bundle("failed", "failed", "2026-04-01")
        self.make_bundle("e2e", created="2026-05-01", kind="e2e")
        self.assertEqual(query.run_dir("queries"), complete)
        self.assertEqual(query.run_dir(str(self.recipe)), complete)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(query.run_dir("queries", "interrupted").name, "interrupted")
        self.assertIn("bench inspect", stderr.getvalue())

    def test_reports_reject_explicit_incomplete_modern_runs(self):
        for state in ("running", "interrupted", "failed"):
            directory = self.make_bundle(state, state=state)
            with self.subTest(state=state), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(query.run_dir("queries", state), directory)
                with self.assertRaisesRegex(SystemExit, "Use bench inspect"):
                    query.summary("queries", run=state)

    def test_legacy_unknown_state_requires_explicit_unique_id(self):
        legacy = self.results / "queries" / "old-run"
        legacy.mkdir(parents=True)
        (legacy / "run.json").write_text(json.dumps({"experiment": "queries", "run_id": "old-run"}))
        with self.assertRaisesRegex(SystemExit, "no complete"):
            query.run_dir("queries")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(query.run_dir("queries", "old"), legacy)
        self.assertIn("unknown", stderr.getvalue())
        self.make_bundle("old-another")
        with self.assertRaisesRegex(SystemExit, "ambiguous"):
            query.run_dir("queries", "old")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(query.run_dir("queries", "old-run"), legacy)

    def test_summary_and_check_read_named_selection(self):
        selected = self.root / "selected"
        self.write_program(selected / "a")
        with mock.patch("harness.results.load_query_selection", return_value=selected, create=True) as loader:
            with mock.patch.object(query, "run_dir", side_effect=AssertionError("must use selection")):
                query.summary(str(self.recipe), selection="paper")
                self.assertEqual(query.check("queries", selection="paper"), 0)
        self.assertEqual(loader.call_args_list, [mock.call("queries", "paper"), mock.call("queries", "paper")])
        with self.assertRaisesRegex(SystemExit, "either --selection or --run"):
            query.check("queries", run="specific", selection="paper")


if __name__ == "__main__":
    unittest.main()
