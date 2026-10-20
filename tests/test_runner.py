import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from harness import artifacts, config, runner

REAL_RUN_ONE = runner.run_one
REAL_EGGLOG_BUILD = Path(__file__).resolve().parent.parent / "systems" / "egglog" / "build"


class RunBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            "ROOT": self.root, "BENCHMARKS": self.root / "benchmarks",
            "SYSTEMS": self.root / "systems", "EXPERIMENTS": self.root / "experiments",
            "RESULTS": self.root / "results", "RUNS": self.root / "runs",
        }.items():
            self.stack.enter_context(mock.patch.object(config, name, value))
        self.stack.enter_context(mock.patch.object(runner, "ROOT", self.root))
        self.stack.enter_context(mock.patch.object(runner, "host_info", return_value={
            "hostname": "test-host", "cpu": "cpu", "ncpu": 8, "cpu_affinity": [0, 1, 2, 3],
        }))
        self.stack.enter_context(mock.patch.object(artifacts, "_provenance", return_value={
            "harness_commit": "harness-commit", "toolchain": {"python": "test-version"},
        }))
        self.stack.enter_context(mock.patch.object(runner, "repo_commit", return_value="harness-commit"))
        self.stack.enter_context(mock.patch.object(runner, "memory_cap_prefix", return_value=["prlimit", "--"]))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        suite = config.BENCHMARKS / "suite"
        suite.mkdir(parents=True)
        (suite / "a.egg").write_text('(include "header.egg")\n; (include "unused.egg")\n')
        (suite / "b.egg").write_text('(include "header.egg")\n')
        (suite / "header.egg").write_text("(sort Expr)\n")
        (suite / "suite.toml").write_text('exclude = ["header.egg"]\n')
        self.system = config.SYSTEMS / "test"
        (self.system / "patches").mkdir(parents=True)
        (self.system / "build").write_text("build source\n")
        (self.system / "run").write_text("adapter source\n")
        (self.system / "patches" / "fix.patch").write_text("patch contents\n")
        (self.root / "harness").mkdir()
        for name in ("runner.py", "config.py", "__init__.py"):
            (self.root / "harness" / name).write_text("# measurement source\n")
        self.exp = config.Experiment(
            "example", "test", ["suite"],
            [config.Variant("normal", "test", "branch", patches=("fix",), threads=(4,))],
            threads=[1], reps=2, timeout=10, memory_gb=1, cpus="0-3", baseline="normal",
            speedup_labels={"normal": "display name"},
        )
        self.builder = self.stack.enter_context(mock.patch.object(runner, "build", return_value=runner.Build(
            self.root / "build" / "test", {"commit": "a" * 40, "system": "test"},
        )))
        self.executor = self.stack.enter_context(mock.patch.object(runner, "run_one", return_value={
            "status": "ok", "wall_s": 1.25, "report": {},
        }))

    def run_bundle(self, **kwargs):
        path = runner.run_experiment(self.exp, **kwargs)
        manifest = json.loads((path / "run.json").read_text())
        rows = [json.loads(line) for line in (path / "measurements.jsonl").read_text().splitlines()]
        return path, manifest, rows

    def test_overrides_provenance_and_portable_bundle(self):
        path, manifest, rows = self.run_bundle(
            only_benchmarks=["suite/b"], threads=[2], reps=3, timeout=7,
            memory_gb=0, cpus="2-3", machine="lab-machine", tag="trial",
        )
        self.assertEqual(path.parent, config.RUNS)
        self.assertFalse(config.RESULTS.exists())
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(manifest["machine_id"], "lab-machine")
        self.assertEqual(manifest["config"]["benchmarks"], ["suite/b"])
        self.assertEqual(manifest["config"]["variants"][0]["threads"], [2])
        self.assertEqual(manifest["config"]["speedup_labels"], {"normal": "display name"})
        self.assertEqual(manifest["cases"], [{
            "suite": "suite", "benchmark": "b", "variant": "normal", "threads": 2, "reps": 3,
            "timing_scope": "process",
        }])
        self.assertEqual(manifest["completion"]["planned_repetitions"], 3)
        self.assertEqual(manifest["completion"]["recorded_repetitions"], 3)
        self.assertEqual(manifest["measurement_policy"]["memory_cap_method"], "none")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["commit"], "a" * 40)
        self.assertEqual(rows[0]["environment_id"], manifest["environment_id"])
        self.assertEqual(rows[0]["input_id"], manifest["inputs"]["suite/b"]["input_id"])
        self.assertEqual(rows[0]["build_id"], manifest["builds"]["normal"]["build_id"])
        self.assertTrue(rows[0]["log_dir"].startswith("logs/"))
        self.assertFalse(Path(rows[0]["log_dir"]).is_absolute())
        snapshot = manifest["builds"]["normal"]["patch_snapshots"]["patches/test/fix.patch"]
        self.assertEqual((path / snapshot["path"]).read_text(), "patch contents\n")
        self.assertEqual(snapshot["sha256"], rows[0]["patch_hashes"][0])
        inputs = manifest["inputs"]["suite/b"]["source_snapshots"]
        self.assertEqual((path / inputs["inputs/suite/b/header.egg"]["path"]).read_text(), "(sort Expr)\n")
        self.assertIn("inputs/suite/b/b.egg", inputs)
        self.assertIn("inputs/suite/b/suite.toml", inputs)
        self.assertEqual(self.executor.call_args.args[3:7], (2, 7, 0, "2-3"))
        self.assertEqual(artifacts.verify_run(path)["state"], "complete")
        frozen = config.experiment_from_dict(manifest["experiment"], manifest["config"])
        self.assertEqual(frozen.variants[0].threads, (2,))

    def test_runtime_env_is_frozen_recorded_and_changes_measurement_identity(self):
        original = self.exp.variants[0]
        self.exp.variants = [replace(original, env={"QUERY_DEPTH": "2", "TOP_PARTITION": "1"})]
        _, first_manifest, first = self.run_bundle(only_benchmarks=["suite/a"], reps=1)
        self.assertEqual(first_manifest["config"]["variants"][0]["env"], self.exp.variants[0].env)
        self.assertEqual(first_manifest["builds"]["normal"]["env"], self.exp.variants[0].env)
        self.assertEqual(first[0]["env"], self.exp.variants[0].env)
        frozen = config.experiment_from_dict("example", first_manifest["config"])
        self.assertEqual(frozen.variants[0].env, self.exp.variants[0].env)
        self.exp.variants = [replace(original, env={"TOP_PARTITION": "1", "QUERY_DEPTH": "2"})]
        _, _, reordered = self.run_bundle(only_benchmarks=["suite/a"], reps=1)
        self.assertEqual(first[0]["build_id"], reordered[0]["build_id"])
        self.exp.variants = [replace(original, env={"QUERY_DEPTH": "0", "TOP_PARTITION": "1"})]
        _, changed_manifest, changed = self.run_bundle(only_benchmarks=["suite/a"], reps=1)
        self.assertNotEqual(first[0]["build_id"], changed[0]["build_id"])
        self.assertNotEqual(first[0]["config_id"], changed[0]["config_id"])
        self.assertEqual(first[0]["input_id"], changed[0]["input_id"])
        self.assertEqual(first_manifest["environment_id"], changed_manifest["environment_id"])

    def test_frozen_benchmark_timing_survives_suite_changes_and_has_identity(self):
        self.exp.benchmark_timing = {"suite/a": "analysis"}
        _, first_manifest, first = self.run_bundle(only_benchmarks=["suite/a"], reps=1)
        self.assertEqual(first_manifest["inputs"]["suite/a"]["egglog_timing"], "analysis")
        self.assertEqual(first_manifest["config"]["benchmark_timing"], {"suite/a": "analysis"})
        self.exp = config.experiment_from_dict("example", first_manifest["config"])
        # The live suite still defaults to process; the frozen config must win.
        self.assertEqual(config.load_suite("suite")[0].egglog_timing, "process")
        _, repeated_manifest, repeated = self.run_bundle(reps=1)
        self.assertEqual(self.executor.call_args.args[2].egglog_timing, "analysis")
        self.assertEqual(first[0]["input_id"], repeated[0]["input_id"])
        self.exp.benchmark_timing = {"suite/a": "process"}
        _, changed_manifest, changed = self.run_bundle(reps=1)
        self.assertNotEqual(first[0]["input_id"], changed[0]["input_id"])
        self.assertNotEqual(first[0]["config_id"], changed[0]["config_id"])
        self.assertEqual(repeated_manifest["environment_id"], changed_manifest["environment_id"])

    def test_interactive_helper_is_snapshotted_and_fingerprinted(self):
        helper = self.system / "interactive.py"
        helper.write_text("# original protocol\n")
        bundle, first_manifest, first = self.run_bundle(only_benchmarks=["suite/a"], reps=1)
        source = first_manifest["builds"]["normal"]["adapter_snapshots"]["adapters/test/interactive.py"]
        self.assertEqual((bundle / source["path"]).read_text(), "# original protocol\n")
        helper.write_text("# revised protocol\n")
        _, _, changed = self.run_bundle(only_benchmarks=["suite/a"], reps=1)
        self.assertNotEqual(first[0]["build_id"], changed[0]["build_id"])

    def test_run_one_propagates_runtime_env_and_analysis_policy(self):
        system = config.SYSTEMS / "egglog"
        system.mkdir()
        (system / "build").write_text("# test build\n")
        adapter = system / "run"
        adapter.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
out = Path(sys.argv[5])
(out / "observed.json").write_text(json.dumps({name: os.environ.get(name) for name in ("QUERY_DEPTH", "BENCH_EGGLOG_TIMING")}))
(out / "report.json").write_text(json.dumps({"wall_s": 0.01, "timing_scope": "analysis"}))
''')
        adapter.chmod(0o755)
        bench = replace(config.resolve_benchmarks(["suite/a"])[0], egglog_timing="analysis")
        variant = replace(self.exp.variants[0], system="egglog", env={"QUERY_DEPTH": "0"})
        log_dir = self.root / "env-logs"
        with mock.patch.dict(os.environ, {"QUERY_DEPTH": "2", "BENCH_EGGLOG_TIMING": "process"}):
            result = REAL_RUN_ONE(variant, self.builder.return_value, bench, 1, 10, 0, None, log_dir)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["timing_scope"], "analysis")
        self.assertEqual(json.loads((log_dir / "observed.json").read_text()), {"QUERY_DEPTH": "0", "BENCH_EGGLOG_TIMING": "analysis"})
        self.assertEqual(json.loads((log_dir / "env.json").read_text()), {"QUERY_DEPTH": "0"})

    def test_failed_or_missing_analysis_report_keeps_intended_scope(self):
        system = config.SYSTEMS / "egglog"
        system.mkdir()
        (system / "build").write_text("# test build\n")
        adapter = system / "run"
        bench = replace(config.resolve_benchmarks(["suite/a"])[0], egglog_timing="analysis")
        variant = replace(self.exp.variants[0], system="egglog")
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                adapter.write_text(f"#!/bin/sh\nexit {exit_code}\n")
                adapter.chmod(0o755)
                result = REAL_RUN_ONE(variant, self.builder.return_value, bench, 1, 10, 0, None, self.root / f"missing-{exit_code}")
                self.assertEqual(result["status"], "fail")
                self.assertEqual(result["timing_scope"], "analysis")

    def test_invalid_runtime_environment_is_rejected_before_output(self):
        for env in ({"QUERY_DEPTH": 0}, {"BAD=NAME": "value"}, {"NAME": "bad\0value"}):
            self.exp.variants = [replace(self.exp.variants[0], env=env)]
            with self.subTest(env=env), self.assertRaises(SystemExit):
                self.run_bundle()
        self.assertFalse(config.RUNS.exists())

    def test_interruption_preserves_completed_measurements(self):
        self.executor.side_effect = [{"status": "ok", "wall_s": 1, "report": {}}, KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            runner.run_experiment(self.exp)
        path = next(config.RUNS.iterdir())
        manifest = artifacts.verify_run(path)
        self.assertEqual(manifest["state"], "interrupted")
        self.assertEqual(manifest["completion"]["recorded_repetitions"], 1)
        self.assertEqual(len((path / "measurements.jsonl").read_text().splitlines()), 1)

    def test_run_one_interrupt_terminates_the_child_group(self):
        # Call the original function while run_experiment tests use the executor mock.
        child = mock.Mock(pid=12345, returncode=None)
        bench = config.resolve_benchmarks(["suite/a"])[0]
        with mock.patch.object(runner.subprocess, "Popen", return_value=child), \
             mock.patch.object(runner.os, "wait4", side_effect=[KeyboardInterrupt(), (12345, 9, None)]), \
             mock.patch.object(runner.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                REAL_RUN_ONE(self.exp.variants[0], self.builder.return_value, bench,
                             1, 10, 0, None, self.root / "child-logs")
        kill.assert_called_once_with(12345, runner.signal.SIGKILL)
        self.assertEqual(child.returncode, -9)

    def test_build_failure_is_sealed_as_failed(self):
        self.builder.side_effect = SystemExit("build failed")
        with self.assertRaises(SystemExit):
            runner.run_experiment(self.exp)
        path = next(config.RUNS.iterdir())
        manifest = artifacts.verify_run(path)
        self.assertEqual(manifest["state"], "failed")
        self.assertIn("build failed", manifest["error"])
        self.assertEqual(manifest["completion"]["recorded_repetitions"], 0)

    def test_terminal_failure_finishes_case_without_repeating(self):
        self.executor.return_value = {"status": "timeout", "wall_s": 10, "report": {}}
        _, manifest, rows = self.run_bundle(only_benchmarks=["suite/a"], reps=5)
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(len(rows), 1)
        self.assertEqual(manifest["completion"], {
            "planned_repetitions": 5, "recorded_repetitions": 1, "terminal_cases": 1,
        })

    def test_identity_tracks_inputs_patches_and_adapters(self):
        kwargs = {"only_benchmarks": ["suite/a"], "reps": 1}
        _, original_manifest, original = self.run_bundle(**kwargs)
        (config.BENCHMARKS / "suite" / "header.egg").write_text("(sort Different)\n")
        _, _, changed = self.run_bundle(**kwargs)
        self.assertNotEqual(original[0]["input_id"], changed[0]["input_id"])
        self.assertNotEqual(original[0]["config_id"], changed[0]["config_id"])
        self.assertEqual(original[0]["build_id"], changed[0]["build_id"])
        self.assertEqual(original[0]["environment_id"], changed[0]["environment_id"])
        (self.system / "patches" / "fix.patch").write_text("changed patch\n")
        _, _, patched = self.run_bundle(**kwargs)
        self.assertNotEqual(changed[0]["build_id"], patched[0]["build_id"])
        (self.system / "run").write_text("changed adapter\n")
        _, _, adapted = self.run_bundle(**kwargs)
        self.assertNotEqual(patched[0]["build_id"], adapted[0]["build_id"])
        # Selection and presentation settings do not change measurement identity.
        self.exp.description = "another description"
        self.exp.plot = "scaling"
        self.exp.baseline = None
        self.exp.speedup_labels = {"normal": "renamed display"}
        _, final_manifest, display = self.run_bundle(**kwargs, tag="display")
        self.assertEqual(adapted[0]["config_id"], display[0]["config_id"])
        self.assertEqual(original_manifest["environment_id"], final_manifest["environment_id"])

    def test_facts_change_input_identity(self):
        facts = config.BENCHMARKS / "suite" / "facts"
        facts.mkdir()
        (facts / "R.tsv").write_text("1\n")
        bench = config.Benchmark("suite", "a", config.BENCHMARKS / "suite" / "a.egg", facts)
        before = runner.benchmark_identity(bench, {})
        (facts / "R.tsv").write_text("2\n")
        self.assertNotEqual(before["input_id"], runner.benchmark_identity(bench, {})["input_id"])

    def test_executable_hash_follows_symlink_contents(self):
        directory = self.root / "binary-build"
        directory.mkdir()
        executable = directory / "actual-binary"
        executable.write_bytes(b"first build")
        (directory / "egglog").symlink_to(executable)
        built = runner.Build(directory, {})
        first, _ = runner.executable_identities("egglog", built, [])
        executable.write_bytes(b"second build")
        second, _ = runner.executable_identities("egglog", built, [])
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_machine_and_limits_change_environment_identity(self):
        _, first, _ = self.run_bundle(only_benchmarks=["suite/a"], reps=1, machine="first")
        _, second, _ = self.run_bundle(only_benchmarks=["suite/b"], reps=3, machine="first")
        self.assertEqual(first["environment_id"], second["environment_id"])
        _, other_machine, _ = self.run_bundle(reps=1, machine="second")
        self.assertNotEqual(first["environment_id"], other_machine["environment_id"])
        _, other_limits, _ = self.run_bundle(reps=1, machine="first", timeout=20)
        self.assertNotEqual(first["environment_id"], other_limits["environment_id"])

    def test_invalid_settings_create_no_outputs(self):
        for kwargs in ({"threads": [0]}, {"threads": []}, {"reps": 0}, {"timeout": 0},
                       {"timeout": float("nan")}, {"memory_gb": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SystemExit):
                runner.run_experiment(self.exp, **kwargs)
        self.assertFalse(config.RUNS.exists())

    def test_explicit_config_path_keeps_stem_and_source(self):
        path = self.root / "scratch" / "custom.toml"
        path.parent.mkdir()
        path.write_text('benchmarks = ["suite/a"]\n[[variants]]\nlabel = "normal"\nsystem = "test"\nref = "main"\n')
        exp = config.load_experiment(str(path))
        self.assertEqual(exp.name, "custom")
        self.assertEqual(exp.source_path, path)
        self.assertEqual(config.experiment_path("example"), config.EXPERIMENTS / "example.toml")

    def test_recipe_relative_patches_and_frozen_config(self):
        directory = self.root / "scratch" / "one off experiment"
        directory.mkdir(parents=True)
        local_patch = directory / "new fix.patch"
        local_patch.write_text("new patch contents\n")
        recipe = directory / "recipe.toml"
        recipe.write_text('benchmarks = ["suite/a"]\nreps = 1\n[[variants]]\n'
                          'label = "normal"\nsystem = "test"\nref = "main"\n'
                          'patches = ["fix", "new fix.patch"]\n')
        self.exp = config.load_experiment(recipe)
        self.assertEqual(self.exp.variants[0].patches, ("fix", str(local_patch)))
        path, manifest, rows = self.run_bundle()
        self.assertEqual(self.builder.call_args.args[2], ("fix", str(local_patch)))
        snapshots = manifest["builds"]["normal"]["patch_snapshots"]
        self.assertEqual((path / snapshots["patches/test/explicit-1-new fix.patch"]["path"]).read_text(),
                         "new patch contents\n")
        local_patch.unlink()
        frozen = config.experiment_from_dict("recipe", manifest["config"])
        self.assertEqual(frozen.variants[0].patches, ("fix", str(local_patch)))
        self.assertEqual(len(rows[0]["patch_hashes"]), 2)

    def test_patch_filename_does_not_change_content_identity(self):
        patch = self.root / "external patch.patch"
        patch.write_text("patch contents\n")
        _, _, named = self.run_bundle(only_benchmarks=["suite/a"], reps=1)
        self.exp.variants = [config.Variant("normal", "test", "branch", patches=(str(patch),), threads=(4,))]
        _, _, explicit = self.run_bundle(only_benchmarks=["suite/a"], reps=1)
        self.assertEqual(named[0]["patch_hashes"], explicit[0]["patch_hashes"])
        self.assertEqual(named[0]["build_id"], explicit[0]["build_id"])
        self.assertEqual(named[0]["config_id"], explicit[0]["config_id"])

    def test_port_identity_is_thread_independent_and_changes_with_sources(self):
        (self.system / "system.toml").write_text('port_dir = "ported"\n')
        ports = config.BENCHMARKS / "suite" / "ported"
        ports.mkdir()
        (ports / "a.rs").write_text("first source\n")
        bundle, manifest, rows = self.run_bundle(only_benchmarks=["suite/a"], threads=[1, 4], reps=1)
        snapshot = manifest["inputs"]["suite/a"]["source_snapshots"]["inputs/suite/a/ports/test/a.rs"]
        self.assertEqual((bundle / snapshot["path"]).read_text(), "first source\n")
        self.assertEqual(rows[0]["port_id"], rows[1]["port_id"])
        self.assertNotEqual(rows[0]["config_id"], rows[1]["config_id"])
        (ports / "a.rs").write_text("second source\n")
        _, _, changed = self.run_bundle(only_benchmarks=["suite/a"], threads=[1], reps=1)
        self.assertNotEqual(rows[0]["port_id"], changed[0]["port_id"])

    def test_egglog_build_accepts_named_and_spaced_patch_paths(self):
        # A cached executable keeps this shell integration test independent of
        # cargo and Git worktrees, while exercising real patch resolution/hash keys.
        system = config.SYSTEMS / "egglog"
        (system / "patches").mkdir(parents=True)
        shutil.copyfile(REAL_EGGLOG_BUILD, system / "build")
        named = system / "patches" / "legacy.patch"
        named.write_bytes(b"named patch\n")
        explicit = self.root / "scratch with spaces" / "local fix.patch"
        explicit.parent.mkdir()
        explicit.write_bytes(b"explicit patch\n")
        (self.root / "egglog").mkdir()
        (self.root / "egglog" / "Cargo.toml").touch()
        commands = self.root / "commands"
        commands.mkdir()
        git = commands / "git"
        git.write_text("#!/bin/sh\nprintf '%s\\n' " + "a" * 40 + "\n")
        git.chmod(0o755)
        digest = hashlib.sha256(named.read_bytes() + explicit.read_bytes()).hexdigest()[:8]
        cached = self.root / "build" / "egglog" / ("a" * 12 + "+" + digest)
        cached.mkdir(parents=True)
        binary = cached / "egglog"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        proc = subprocess.run(
            ["bash", str(system / "build"), "main", "legacy", str(explicit.relative_to(self.root))],
            cwd=self.root, env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]},
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(cached))


if __name__ == "__main__":
    unittest.main()
